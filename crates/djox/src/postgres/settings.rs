//! [`PostgresSettings`]: which server to talk to, how many connections to
//! keep, and how to encrypt them.

use djox_core::{Error, Result};
use tokio_postgres_rustls::MakeRustlsConnect;

use super::tls::{TlsMode, TlsSettings, tls_connector};

/// Which server to talk to, and how. Mirrors Django's `DATABASES[alias]`
/// for Postgres: `HOST`, `PORT`, `NAME`, `USER` and `PASSWORD` all end up in
/// `connection`.
#[derive(Debug, Clone)]
pub struct PostgresSettings {
    /// Where the database is, as a URL (`postgresql://user:password@localhost:5432/app`)
    /// or as libpq's `key=value` words (`host=localhost dbname=app user=django`).
    pub connection: String,
    /// The most connections the pool opens at once.
    pub pool_size: usize,
    /// The session's time zone (`SET TIME ZONE`), like `"UTC"`. Django sets
    /// it on its own connections; djox's must match, because Postgres reads
    /// a date and time without a zone in it.
    pub time_zone: Option<String>,
    /// TLS: libpq's `sslmode`, `sslrootcert`, `sslcert` and `sslkey` (see `tls.rs`).
    pub tls: TlsSettings,
}

impl PostgresSettings {
    /// Settings for `connection`, with a pool of 10 connections.
    pub fn new(connection: impl Into<String>) -> PostgresSettings {
        PostgresSettings {
            connection: connection.into(),
            pool_size: 10,
            time_zone: None,
            tls: TlsSettings::default(),
        }
    }

    /// The driver's connection settings, read from `connection`, with the time zone.
    pub fn driver_config(&self) -> Result<tokio_postgres::Config> {
        // `.parse()` reads the text into a `Config`, like `Config.from_string(...)`.
        let mut config: tokio_postgres::Config = self.connection.parse().map_err(|error| {
            // The driver's reader knows only `sslmode=disable`, `prefer` and
            // `require`, and none of the certificate settings.
            let mut message = format!("can't read the Postgres connection settings: {error}");
            if self.connection.contains("ssl") {
                message.push_str(" (give sslmode, sslrootcert, sslcert and sslkey as separate settings)");
            }
            Error::Settings(message)
        })?;
        let tls_mode = self
            .tls
            .mode
            .unwrap_or(TlsMode::from_driver_mode(config.get_ssl_mode()));
        config.ssl_mode(tls_mode.driver_mode());
        if let Some(time_zone) = &self.time_zone {
            // `-c name=value` sets a setting for the session as it starts, so
            // it costs no extra round trip (libpq's `options`).
            // Added to any `options` the settings already have.
            let options = match config.get_options() {
                Some(given) => format!("{given} -c TimeZone={time_zone}"),
                None => format!("-c TimeZone={time_zone}"),
            };
            config.options(options);
        }
        // Shown in `pg_stat_activity`, unless the settings already name one.
        if config.get_application_name().is_none() {
            config.application_name("djox");
        }
        Ok(config)
    }

    /// How much TLS to use: the `sslmode` setting, or else the connection
    /// text's, or else `prefer` (libpq's default).
    pub fn tls_mode(&self) -> Result<TlsMode> {
        match self.tls.mode {
            Some(mode) => Ok(mode),
            None => Ok(TlsMode::from_driver_mode(self.driver_config()?.get_ssl_mode())),
        }
    }

    /// What the driver opens TLS connections with (see `tls.rs`).
    pub fn tls_connector(&self) -> Result<MakeRustlsConnect> {
        tls_connector(&self.tls, self.tls_mode()?)
    }

    /// Where the database is, for `repr()`: `"localhost:5432/app"`. Never the password.
    pub fn describe(&self) -> String {
        let Ok(config) = self.driver_config() else {
            return "?".to_string();
        };
        let mut hosts = Vec::new();
        for host in config.get_hosts() {
            match host {
                tokio_postgres::config::Host::Tcp(name) => hosts.push(name.clone()),
                // A Unix socket's folder, like `/var/run/postgresql`. Only Unix
                // has those sockets, so on Windows this variant doesn't exist:
                // `#[cfg(unix)]` keeps the line only when building for Unix,
                // like an `if sys.platform != "win32":` that runs at compile time.
                #[cfg(unix)]
                tokio_postgres::config::Host::Unix(folder) => hosts.push(folder.display().to_string()),
            }
        }
        let port = config.get_ports().first().copied().unwrap_or(5432);
        let name = config.get_dbname().unwrap_or("");
        format!("{}:{port}/{name}", hosts.join(","))
    }

    /// Fails for settings that can't work.
    pub fn check(&self) -> Result<()> {
        if self.pool_size == 0 {
            return Err(Error::Settings(
                "the Postgres pool needs at least one connection".to_string(),
            ));
        }
        if let Some(time_zone) = &self.time_zone
            && time_zone.contains(char::is_whitespace)
        {
            return Err(Error::Settings(format!("{time_zone:?} is not a time zone name")));
        }
        self.driver_config()?;
        // Reads the certificate files, so a missing one fails now.
        self.tls_connector().map(|_| ())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn reads_urls_and_key_value_words() {
        let from_url = PostgresSettings::new("postgresql://django:secret@db.example.com:6543/app");
        let config = from_url.driver_config().unwrap();
        assert_eq!(config.get_dbname(), Some("app"));
        assert_eq!(config.get_ports(), [6543]);

        let from_words = PostgresSettings::new("host=localhost dbname=app user=django password=secret");
        assert_eq!(from_words.driver_config().unwrap().get_user(), Some("django"));
        assert_eq!(from_words.describe(), "localhost:5432/app", "never the password");
    }

    #[test]
    fn refuses_settings_that_cant_work() {
        let mut settings = PostgresSettings::new("host=localhost dbname=app");
        settings.pool_size = 0;
        assert!(matches!(settings.check(), Err(Error::Settings(_))));
        assert!(matches!(
            PostgresSettings::new("postgresql://[not a host").check(),
            Err(Error::Settings(_))
        ));
    }
}
