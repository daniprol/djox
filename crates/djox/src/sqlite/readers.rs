//! [`ReaderQueue`]: the queue all reader workers share, which hands each read
//! to the reader that went to sleep **last**.
//!
//! # Why not a plain queue
//!
//! With a plain shared queue (like `queue.Queue` with several consumer
//! threads), a new job wakes the reader that has been asleep the *longest*.
//! When queries come one at a time, the readers take turns: each query wakes
//! a thread that has been idle for a while, whose CPU has often gone to sleep
//! and whose caches are cold. On a 4-core VM that cost about 20 µs per query:
//! one reader answered a lone query in 59 µs, four readers took 79 µs.
//!
//! So this queue keeps the sleeping readers on a *stack* and wakes the one
//! that fell asleep last: one-at-a-time queries keep landing on the same warm
//! thread, and the others still help as soon as several queries are waiting.
//!
//! In Python terms it is a `collections.deque` of jobs and a list of sleeping
//! threads, both behind one `threading.Lock`.

use std::collections::VecDeque;
use std::sync::Mutex;
use std::thread::{self, Thread};

use super::worker::Job;

/// The jobs waiting for a reader, and the readers waiting for a job.
pub(crate) struct ReaderQueue {
    /// One lock for both lists, so a job and a sleeping reader can never miss
    /// each other (a job added just as a reader decides to sleep).
    state: Mutex<QueueState>,
}

struct QueueState {
    /// Jobs no reader has taken yet, oldest first.
    jobs: VecDeque<Job>,
    /// The readers that are asleep, the one that fell asleep last at the end.
    /// A `Thread` is a handle to a thread, used to wake it (`unpark`).
    sleeping: Vec<Thread>,
    /// Once closed, new jobs are refused, and readers stop when no job is left.
    closed: bool,
}

impl ReaderQueue {
    /// An empty, open queue.
    pub(crate) fn new() -> ReaderQueue {
        ReaderQueue {
            state: Mutex::new(QueueState {
                jobs: VecDeque::new(),
                sleeping: Vec::new(),
                closed: false,
            }),
        }
    }

    /// Adds a job and wakes the reader that fell asleep last, if any is asleep.
    ///
    /// If the queue is closed, the job is dropped, which answers its caller
    /// with `Error::Closed` (see `ReplySender`'s `Drop`).
    pub(crate) fn send(&self, job: Job) {
        let mut state = self.state.lock().unwrap();
        if state.closed {
            return;
        }
        state.jobs.push_back(job);
        // `.pop()` takes the last item, like Python's `list.pop()`.
        if let Some(reader) = state.sleeping.pop() {
            reader.unpark();
        }
    }

    /// Waits for the next job, for a reader thread. Gives `None` once the
    /// queue is closed and empty: the reader should stop.
    pub(crate) fn next_job(&self) -> Option<Job> {
        loop {
            // A block `{ ... }` limits how long the lock is held: it is let go
            // at the closing brace, before the thread goes to sleep.
            {
                let mut state = self.state.lock().unwrap();
                if let Some(job) = state.jobs.pop_front() {
                    return Some(job);
                }
                if state.closed {
                    return None;
                }
                let this_thread = thread::current();
                // A thread can wake up without being called (the OS allows it), and
                // then still be on the list: take it off before adding it again.
                state.sleeping.retain(|reader| reader.id() != this_thread.id());
                state.sleeping.push(this_thread);
            }
            // `park` sleeps until another thread calls `unpark` on this one. If
            // `unpark` came first, it returns right away, so no wake-up is lost.
            thread::park();
        }
    }

    /// Refuses new jobs and wakes every reader. Jobs already queued still run.
    pub(crate) fn close(&self) {
        let mut state = self.state.lock().unwrap();
        state.closed = true;
        for reader in state.sleeping.drain(..) {
            reader.unpark();
        }
    }
}

#[cfg(test)]
mod tests {
    use std::sync::Arc;
    use std::time::{Duration, Instant};

    use djox_core::{Error, Reply, Result, Rows, Value};

    use super::*;

    impl ReaderQueue {
        /// How many readers are asleep right now.
        fn sleeping_count(&self) -> usize {
            self.state.lock().unwrap().sleeping.len()
        }
    }

    /// Starts `count` reader threads that answer each job with their own name.
    fn start_readers(queue: &Arc<ReaderQueue>, count: usize) -> Vec<thread::JoinHandle<()>> {
        let mut readers = Vec::new();
        for number in 0..count {
            let queue = Arc::clone(queue);
            let reader = thread::Builder::new().name(format!("reader-{number}"));
            readers.push(
                reader
                    .spawn(move || {
                        while let Some(Job::FetchAll { reply, .. }) = queue.next_job() {
                            let name = thread::current().name().unwrap().to_string();
                            reply.send(Ok(Rows {
                                columns: vec!["reader".to_string()],
                                rows: vec![vec![Value::Text(name)]],
                            }));
                        }
                    })
                    .unwrap(),
            );
        }
        readers
    }

    /// Sends a job and gives back the reply to wait for.
    fn send_job(queue: &ReaderQueue) -> Reply<Rows> {
        let (sender, reply) = Reply::new();
        queue.send(Job::FetchAll {
            sql: "SELECT 1".to_string(),
            parameters: vec![],
            reply: sender,
        });
        reply
    }

    /// The name of the reader that answered.
    fn answered_by(answer: Result<Rows>) -> Value {
        answer.unwrap().rows[0][0].clone()
    }

    /// Waits (a little at a time, for at most 5 seconds) until `count` readers are asleep.
    fn wait_until_asleep(queue: &ReaderQueue, count: usize) {
        let started = Instant::now();
        while queue.sleeping_count() < count {
            assert!(
                started.elapsed() < Duration::from_secs(5),
                "the readers never fell asleep"
            );
            thread::sleep(Duration::from_millis(1));
        }
    }

    #[test]
    fn one_job_at_a_time_goes_to_the_reader_that_slept_last() {
        let queue = Arc::new(ReaderQueue::new());
        let readers = start_readers(&queue, 4);

        let mut answered = Vec::new();
        for _ in 0..20 {
            wait_until_asleep(&queue, 4);
            answered.push(answered_by(send_job(&queue).wait_blocking()));
        }

        // A plain queue would hand the jobs round to all four readers.
        assert!(answered.iter().all(|reader| *reader == answered[0]), "{answered:?}");
        queue.close();
        for reader in readers {
            reader.join().unwrap();
        }
    }

    #[test]
    fn many_jobs_at_once_all_get_answered() {
        let queue = Arc::new(ReaderQueue::new());
        let readers = start_readers(&queue, 4);

        let mut replies = Vec::new();
        for _ in 0..1000 {
            replies.push(send_job(&queue));
        }
        for reply in replies {
            assert!(reply.wait_blocking().is_ok());
        }
        queue.close();
        for reader in readers {
            reader.join().unwrap();
        }
    }

    #[test]
    fn closing_runs_queued_jobs_and_refuses_new_ones() {
        let queue = Arc::new(ReaderQueue::new());
        // Queued before any reader exists, then closed.
        let queued = send_job(&queue);
        queue.close();
        let refused = send_job(&queue);

        let readers = start_readers(&queue, 2);
        assert!(queued.wait_blocking().is_ok());
        assert_eq!(refused.wait_blocking().unwrap_err(), Error::Closed);
        // Closed and empty: every reader stops.
        for reader in readers {
            reader.join().unwrap();
        }
    }
}
