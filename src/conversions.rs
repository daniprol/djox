//! Django's most common SQLite converters, done in Rust, for the fast lane.
//!
//! SQLite has no date or decimal types. Django stores a `datetime` as text
//! (`"2020-01-01 09:30:00"`) and a `Decimal` as a number SQLite keeps as a
//! float (`9.99`), and its converters turn them back when rows are read:
//!
//! | Django's field   | Stored as                  | Django's converter (Python)                      |
//! |------------------|----------------------------|--------------------------------------------------|
//! | `DateTimeField`  | `"2020-01-01 09:30:00"`    | `parse_datetime()`, then `make_aware()` with `USE_TZ` |
//! | `DateField`      | `"2020-01-01"`             | `parse_date()`                                   |
//! | `TimeField`      | `"09:30:00"`               | `parse_time()`                                   |
//! | `DecimalField`   | `9.99` (a float) or `10`   | `Decimal`, 15 digits, then `quantize()` to the field's places |
//! | `BooleanField`   | `1` or `0`                 | `bool(value)`                                    |
//!
//! Those converters are a few Python function calls per value, and for big
//! results they are most of the time spent building instances. Here the same
//! work is plain Rust: read the digits, then make the Python object once.
//!
//! Rust only handles the values Django itself writes. Anything else (a date
//! written by hand as `"2020-01-01T09:30+02:00"`, a decimal with more digits
//! than the field allows) gets `None` from `convert()`, and the caller runs
//! Django's own converter, so unusual values come out, or fail, exactly as
//! in Django.

use pyo3::prelude::*;
use pyo3::types::{PyFloat, PyInt, PyString, PyTuple};

/// One of the converters above, chosen by Python (`djox._fast_lane.conversions`)
/// after checking that Django would use exactly that converter for the column.
pub enum Conversion {
    /// Text to `datetime.datetime`. `timezone` is the `tzinfo` Django's
    /// `make_aware()` would add (`USE_TZ = True`), or Python's `None`.
    DateTime { timezone: Py<PyAny> },
    /// Text to `datetime.date`.
    Date,
    /// Text to `datetime.time`.
    Time,
    /// A number to `decimal.Decimal`, with the field's `decimal_places`, and
    /// at most `max_digits` digits (more is an error, which Django raises).
    Decimal { decimal_places: u32, max_digits: u32 },
    /// `1` or `0` to `True` or `False`.
    Boolean,
}

/// The Python classes the conversions make, imported once.
pub struct PythonClasses {
    datetime: Py<PyAny>,
    date: Py<PyAny>,
    time: Py<PyAny>,
    decimal: Py<PyAny>,
}

impl PythonClasses {
    /// Imports `datetime.datetime`, `datetime.date`, `datetime.time` and `decimal.Decimal`.
    pub fn import(py: Python<'_>) -> PyResult<Self> {
        let datetime_module = py.import("datetime")?;
        Ok(PythonClasses {
            datetime: datetime_module.getattr("datetime")?.unbind(),
            date: datetime_module.getattr("date")?.unbind(),
            time: datetime_module.getattr("time")?.unbind(),
            decimal: py.import("decimal")?.getattr("Decimal")?.unbind(),
        })
    }
}

impl Conversion {
    /// Reads Python's description of a conversion: `("datetime", timezone)`,
    /// `("date",)`, `("time",)`, `("decimal", decimal_places, max_digits)` or
    /// `("boolean",)`.
    pub fn from_python(description: &Bound<'_, PyTuple>) -> PyResult<Self> {
        let kind: String = description.get_item(0)?.extract()?;
        // `match` on a string is like a chain of `if kind == "datetime": ... elif ...`.
        match kind.as_str() {
            "datetime" => Ok(Conversion::DateTime {
                timezone: description.get_item(1)?.unbind(),
            }),
            "date" => Ok(Conversion::Date),
            "time" => Ok(Conversion::Time),
            "decimal" => Ok(Conversion::Decimal {
                decimal_places: description.get_item(1)?.extract()?,
                max_digits: description.get_item(2)?.extract()?,
            }),
            "boolean" => Ok(Conversion::Boolean),
            _ => Err(pyo3::exceptions::PyValueError::new_err(format!(
                "unknown conversion {kind:?}"
            ))),
        }
    }

    /// `value` converted the way Django's converter would, or `None` when
    /// this value is one Rust leaves to Django's converter (see the module docs).
    ///
    /// The return type `PyResult<Option<...>>` reads "a Python error, or
    /// maybe a value": `Ok(None)` means "ask Django".
    pub fn convert<'py>(
        &self,
        py: Python<'py>,
        value: &Bound<'py, PyAny>,
        classes: &PythonClasses,
    ) -> PyResult<Option<Bound<'py, PyAny>>> {
        // Every converter of Django's keeps `None` (SQL `NULL`) as it is.
        if value.is_none() {
            return Ok(Some(value.clone()));
        }
        match self {
            Conversion::DateTime { timezone } => {
                let Some(text) = text_of(value) else { return Ok(None) };
                let Some((date, time)) = parse_datetime(&text) else {
                    return Ok(None);
                };
                let arguments = (
                    date.year,
                    date.month,
                    date.day,
                    time.hour,
                    time.minute,
                    time.second,
                    time.microsecond,
                    timezone.bind(py),
                );
                // A date like February 30th fails in Python: Django's converter then raises its error.
                Ok(classes.datetime.bind(py).call1(arguments).ok())
            }
            Conversion::Date => {
                let Some(text) = text_of(value) else { return Ok(None) };
                let Some(date) = parse_date(&text) else { return Ok(None) };
                Ok(classes.date.bind(py).call1((date.year, date.month, date.day)).ok())
            }
            Conversion::Time => {
                let Some(text) = text_of(value) else { return Ok(None) };
                let Some(time) = parse_time(&text) else { return Ok(None) };
                let arguments = (time.hour, time.minute, time.second, time.microsecond);
                Ok(classes.time.bind(py).call1(arguments).ok())
            }
            Conversion::Decimal {
                decimal_places,
                max_digits,
            } => {
                let text = if value.is_exact_instance_of::<PyFloat>() {
                    let number: f64 = value.extract()?;
                    decimal_text_from_float(number, *decimal_places, *max_digits)
                } else if value.is_exact_instance_of::<PyInt>() {
                    // An `i64` holds every integer SQLite stores; a bigger one goes to Django.
                    match value.extract::<i64>() {
                        Ok(number) => decimal_text_from_integer(number, *decimal_places, *max_digits),
                        Err(_) => None,
                    }
                } else {
                    None
                };
                let Some(text) = text else { return Ok(None) };
                Ok(Some(classes.decimal.bind(py).call1((text,))?))
            }
            Conversion::Boolean => {
                if !value.is_exact_instance_of::<PyInt>() {
                    return Ok(None);
                }
                // `match` with ranges of values, like `if number == 1: ... elif number == 0: ...`.
                match value.extract::<i64>() {
                    Ok(1) => Ok(Some(true.into_pyobject(py)?.to_owned().into_any())),
                    Ok(0) => Ok(Some(false.into_pyobject(py)?.to_owned().into_any())),
                    _ => Ok(None),
                }
            }
        }
    }
}

/// The text of `value` if it is a Python `str` (not a subclass), else `None`.
fn text_of(value: &Bound<'_, PyAny>) -> Option<String> {
    if !value.is_exact_instance_of::<PyString>() {
        return None;
    }
    value.extract().ok()
}

/// A date read from text, before Python checks it (February 30th passes here).
#[derive(Debug, PartialEq)] // `derive` writes `==` and a debug print for us, for the tests below.
struct DateParts {
    year: i32,
    month: u8,
    day: u8,
}

/// A time of day read from text.
#[derive(Debug, PartialEq)]
struct TimeParts {
    hour: u8,
    minute: u8,
    second: u8,
    microsecond: u32,
}

/// `"2020-01-31"`, the only way Django writes a date.
fn parse_date(text: &str) -> Option<DateParts> {
    let bytes = text.as_bytes();
    if bytes.len() != 10 || bytes[4] != b'-' || bytes[7] != b'-' {
        return None;
    }
    Some(DateParts {
        year: digits_value(&bytes[0..4])? as i32,
        month: digits_value(&bytes[5..7])? as u8,
        day: digits_value(&bytes[8..10])? as u8,
    })
}

/// `"09:30:00"` or `"09:30:00.123456"`, the ways Django writes a time
/// (one to six digits of a second, like Django's `parse_time()` accepts).
fn parse_time(text: &str) -> Option<TimeParts> {
    let bytes = text.as_bytes();
    if bytes.len() < 8 || bytes[2] != b':' || bytes[5] != b':' {
        return None;
    }
    let mut microsecond = 0;
    if bytes.len() > 8 {
        let fraction = &bytes[9..];
        if bytes[8] != b'.' || fraction.is_empty() || fraction.len() > 6 {
            return None;
        }
        // `".5"` is half a second: pad to six digits, `500000` microseconds.
        microsecond = digits_value(fraction)? * 10_u32.pow(6 - fraction.len() as u32);
    }
    Some(TimeParts {
        hour: digits_value(&bytes[0..2])? as u8,
        minute: digits_value(&bytes[3..5])? as u8,
        second: digits_value(&bytes[6..8])? as u8,
        microsecond,
    })
}

/// `"2020-01-31 09:30:00"`, with a space or a `T`, and maybe a fraction of a
/// second: how Django writes a `datetime` (in UTC, without the offset, when `USE_TZ`).
fn parse_datetime(text: &str) -> Option<(DateParts, TimeParts)> {
    // `get(..10)` is `text[:10]`, but gives `None` instead of failing on short text.
    let date = parse_date(text.get(..10)?)?;
    let separator = text.as_bytes().get(10)?;
    if *separator != b' ' && *separator != b'T' {
        return None;
    }
    let time = parse_time(text.get(11..)?)?;
    Some((date, time))
}

/// The number written by ASCII digits, like `int("0930")`, or `None` if
/// one of them isn't a digit.
fn digits_value(digits: &[u8]) -> Option<u32> {
    let mut value: u32 = 0;
    for digit in digits {
        if !digit.is_ascii_digit() {
            return None;
        }
        value = value * 10 + u32::from(digit - b'0');
    }
    Some(value)
}

/// The text of the `Decimal` Django makes from a float that SQLite stored:
/// first 15 significant digits (SQLite keeps no more), then `decimal_places`.
///
/// Django does `Context(prec=15).create_decimal_from_float(value)` and then
/// `quantize()`, both rounding half to even. Rust's `{:.14e}` formatting
/// rounds the float's exact value to 15 significant digits the same way.
fn decimal_text_from_float(value: f64, decimal_places: u32, max_digits: u32) -> Option<String> {
    if !value.is_finite() {
        return None; // `inf` and `nan`: Django's `quantize()` decides
    }
    let scientific = format!("{value:.14e}"); // like "-9.99000000000000e0"
    let (mantissa, exponent) = scientific.split_once('e')?;
    let exponent: i32 = exponent.parse().ok()?;
    let mut digits = Vec::new();
    for character in mantissa.bytes() {
        if character.is_ascii_digit() {
            digits.push(character - b'0');
        }
    }
    // The 15 digits are an integer; the value is that integer times 10 ** (exponent - 14).
    let digits_exponent = exponent - 14;
    with_decimal_places(
        mantissa.starts_with('-'),
        digits,
        digits_exponent,
        decimal_places,
        max_digits,
    )
}

/// The text of `Decimal(value).quantize(...)` for an integer SQLite stored.
///
/// Up to 15 digits only: Django 6.0 and earlier round integers to 15
/// significant digits first, and 6.1 doesn't, so bigger ones go to Django.
fn decimal_text_from_integer(value: i64, decimal_places: u32, max_digits: u32) -> Option<String> {
    if value.unsigned_abs() >= 1_000_000_000_000_000 {
        return None;
    }
    let mut digits = Vec::new();
    for character in value.unsigned_abs().to_string().bytes() {
        digits.push(character - b'0');
    }
    with_decimal_places(value < 0, digits, 0, decimal_places, max_digits)
}

/// The number `digits * 10 ** digits_exponent`, rounded half to even to
/// `decimal_places`, as text like `"-12.30"`; or `None` when it needs more
/// than `max_digits` digits (Django's `quantize()` then raises `InvalidOperation`).
fn with_decimal_places(
    negative: bool,
    mut digits: Vec<u8>,
    digits_exponent: i32,
    decimal_places: u32,
    max_digits: u32,
) -> Option<String> {
    let wanted_exponent = -(decimal_places as i32);
    if digits_exponent > wanted_exponent {
        // Too few places: add zeros, `12.3` -> `12.30`.
        // `resize` grows the list to the new length, filling it with zeros.
        let missing = (digits_exponent - wanted_exponent) as usize;
        digits.resize(digits.len() + missing, 0);
    } else if digits_exponent < wanted_exponent {
        // Too many places: drop the extra digits and round.
        let dropped_count = (wanted_exponent - digits_exponent) as usize;
        if dropped_count > digits.len() {
            digits = vec![0]; // even the first dropped digit is a leading 0: rounds to zero
        } else {
            let kept_count = digits.len() - dropped_count;
            let dropped = digits.split_off(kept_count); // `digits` keeps the first `kept_count`
            let last_kept_is_odd = digits.last().is_some_and(|digit| digit % 2 == 1);
            let rest_is_zero = dropped[1..].iter().all(|digit| *digit == 0);
            let rounds_up = dropped[0] > 5 || (dropped[0] == 5 && (!rest_is_zero || last_kept_is_odd));
            if rounds_up {
                add_one(&mut digits);
            }
        }
    }
    // Leading zeros don't count as digits (`0.05` has one), but keep one digit.
    while digits.len() > 1 && digits[0] == 0 {
        digits.remove(0);
    }
    if digits.is_empty() {
        digits.push(0);
    }
    if digits.len() > max_digits as usize {
        return None;
    }

    // Write the digits with a decimal point `decimal_places` from the end.
    let places = decimal_places as usize;
    while digits.len() <= places {
        digits.insert(0, 0); // `5` with two places is `0.05`
    }
    let mut text = String::new();
    if negative {
        text.push('-'); // also for zero: Django keeps `Decimal("-0.00")`
    }
    for (position, digit) in digits.iter().enumerate() {
        if position == digits.len() - places {
            text.push('.');
        }
        text.push(char::from(b'0' + digit));
    }
    Some(text)
}

/// Adds one to a number written as digits, carrying like on paper: `[1, 9, 9]` -> `[2, 0, 0]`.
fn add_one(digits: &mut Vec<u8>) {
    for digit in digits.iter_mut().rev() {
        if *digit < 9 {
            *digit += 1;
            return;
        }
        *digit = 0;
    }
    digits.insert(0, 1); // every digit was a 9: `[9, 9]` -> `[1, 0, 0]`
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn dates_are_read_as_django_writes_them() {
        assert_eq!(
            parse_date("2020-01-31"),
            Some(DateParts {
                year: 2020,
                month: 1,
                day: 31
            })
        );
        assert_eq!(parse_date("2020-1-31"), None);
        assert_eq!(parse_date("2020-01-31 "), None);
        assert_eq!(parse_date("2020/01/31"), None);
    }

    #[test]
    fn times_pad_their_fraction_to_microseconds() {
        let time = parse_time("09:30:15.5").unwrap();
        assert_eq!(
            (time.hour, time.minute, time.second, time.microsecond),
            (9, 30, 15, 500_000)
        );
        assert_eq!(parse_time("09:30:15.123456").unwrap().microsecond, 123_456);
        assert_eq!(parse_time("09:30:15.1234567"), None);
        assert_eq!(parse_time("09:30:15."), None);
        assert_eq!(parse_time("09:30"), None);
        assert_eq!(parse_time("09:30:15+02:00"), None);
    }

    #[test]
    fn datetimes_take_a_space_or_a_t() {
        assert!(parse_datetime("2020-01-31 09:30:00").is_some());
        assert!(parse_datetime("2020-01-31T09:30:00.25").is_some());
        assert!(parse_datetime("2020-01-31_09:30:00").is_none());
        assert!(parse_datetime("2020-01-31").is_none());
    }

    #[test]
    fn floats_become_decimals_with_the_fields_places() {
        assert_eq!(decimal_text_from_float(9.99, 2, 6).as_deref(), Some("9.99"));
        assert_eq!(decimal_text_from_float(10.0, 2, 6).as_deref(), Some("10.00"));
        assert_eq!(decimal_text_from_float(0.05, 2, 6).as_deref(), Some("0.05"));
        assert_eq!(decimal_text_from_float(-1.5, 0, 6).as_deref(), Some("-2"));
        assert_eq!(decimal_text_from_float(2.5, 0, 6).as_deref(), Some("2"));
        assert_eq!(decimal_text_from_float(0.001, 2, 6).as_deref(), Some("0.00"));
        assert_eq!(decimal_text_from_float(-0.001, 2, 6).as_deref(), Some("-0.00"));
        assert_eq!(decimal_text_from_float(9999.995, 2, 6).as_deref(), None); // 10000.00 has 7 digits
        assert_eq!(decimal_text_from_float(f64::NAN, 2, 6), None);
    }

    #[test]
    fn integers_become_decimals_with_the_fields_places() {
        assert_eq!(decimal_text_from_integer(10, 2, 6).as_deref(), Some("10.00"));
        assert_eq!(decimal_text_from_integer(0, 2, 6).as_deref(), Some("0.00"));
        assert_eq!(decimal_text_from_integer(-7, 0, 6).as_deref(), Some("-7"));
        assert_eq!(decimal_text_from_integer(1_000_000, 0, 6), None);
        assert_eq!(decimal_text_from_integer(1_000_000_000_000_000, 0, 20), None);
    }

    #[test]
    fn rounding_carries_into_new_digits() {
        assert_eq!(
            with_decimal_places(false, vec![9, 9, 9, 5], -3, 2, 6).as_deref(),
            Some("10.00")
        );
    }
}
