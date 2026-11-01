//! djox values to Postgres parameters, and Postgres columns to djox values.
//!
//! Postgres talks in two formats. In *text* format a value travels as the
//! words you would type in SQL (`2024-05-01`, `12.50`); Postgres reads it
//! with the column type's own input function. In *binary* format it travels
//! as raw bytes (an `integer` is 4 bytes). Each parameter can use either.
//!
//! **Parameters** ([`Parameter`]): numbers, booleans and bytes go in binary,
//! when the column takes them. Everything else goes as text, and Postgres
//! reads it as its column's type. That is how dates, decimals and UUIDs
//! arrive from Python (Django's own SQLite rules turn them into text, see
//! `src/values.rs`), and it means djox never reinterprets a value: Postgres
//! does, exactly as it would read it from `psql`. A number too big for its
//! column goes as text too, so Postgres reports it the way it would for
//! psycopg ("integer out of range", a `DataError`).
//!
//! **Columns** ([`Column`]): `tokio_postgres` reads every result in binary.
//! Each type becomes the `Value` that turns into the Python object psycopg
//! gives Django (see the table in `mod.rs`).

use std::error::Error as StdError;
use std::net::{Ipv4Addr, Ipv6Addr};

use bytes::BytesMut;
use djox_core::Value;
use tokio_postgres::types::{Format, FromSql, IsNull, Kind, ToSql, Type, to_sql_checked};

/// What `ToSql` and `FromSql` return when they fail: any error, boxed.
/// (`Box<dyn Error>` is "some error object", like catching `Exception`.)
type ConversionError = Box<dyn StdError + Sync + Send>;

/// A djox value, ready to be sent as a statement's parameter.
///
/// `tokio_postgres` sends parameters through the `ToSql` trait. `Value` lives
/// in `djox-core`, and Rust only lets a crate implement a trait from another
/// crate for its own types, so this small wrapper is ours. `'a` says it
/// borrows the value (`&'a Value`) instead of copying it.
#[derive(Debug)]
pub struct Parameter<'a>(pub &'a Value);

impl ToSql for Parameter<'_> {
    fn to_sql(&self, column_type: &Type, out: &mut BytesMut) -> Result<IsNull, ConversionError> {
        if is_sent_in_binary(self.0, column_type) {
            return to_binary(self.0, column_type, out);
        }
        match text_of(self.0) {
            Some(text) => {
                out.extend_from_slice(text.as_bytes());
                Ok(IsNull::No)
            }
            None => Ok(IsNull::Yes),
        }
    }

    /// Any value can be offered to any column: what Postgres can't read, it refuses.
    fn accepts(_column_type: &Type) -> bool {
        true
    }

    fn encode_format(&self, column_type: &Type) -> Format {
        if is_sent_in_binary(self.0, column_type) {
            Format::Binary
        } else {
            Format::Text
        }
    }

    // A macro (`!`) that writes the `to_sql_checked` method every `ToSql` needs.
    to_sql_checked!();
}

/// Whether `value` goes in binary to a column of `column_type`: numbers that
/// fit their column, booleans and bytes.
fn is_sent_in_binary(value: &Value, column_type: &Type) -> bool {
    match value {
        Value::Integer(number) => match *column_type {
            Type::INT2 => i16::try_from(*number).is_ok(),
            Type::INT4 => i32::try_from(*number).is_ok(),
            Type::INT8 | Type::FLOAT4 | Type::FLOAT8 => true,
            _ => false,
        },
        Value::Real(_) => matches!(*column_type, Type::FLOAT4 | Type::FLOAT8),
        Value::Boolean(_) => *column_type == Type::BOOL,
        // Bytes are always binary: as text, Postgres would read them as an escaped string.
        Value::Blob(_) => true,
        _ => false,
    }
}

/// Writes a value Postgres expects in binary, with the driver's own encoders.
fn to_binary(value: &Value, column_type: &Type, out: &mut BytesMut) -> Result<IsNull, ConversionError> {
    match (value, column_type) {
        // `as` converts a number to another number type; `is_sent_in_binary` checked that it fits.
        (Value::Integer(number), &Type::INT2) => (*number as i16).to_sql(column_type, out),
        (Value::Integer(number), &Type::INT4) => (*number as i32).to_sql(column_type, out),
        (Value::Integer(number), &Type::INT8) => number.to_sql(column_type, out),
        (Value::Integer(number), &Type::FLOAT4) => (*number as f32).to_sql(column_type, out),
        (Value::Integer(number), &Type::FLOAT8) => (*number as f64).to_sql(column_type, out),
        (Value::Real(number), &Type::FLOAT4) => (*number as f32).to_sql(column_type, out),
        (Value::Real(number), _) => number.to_sql(&Type::FLOAT8, out),
        (Value::Boolean(boolean), _) => boolean.to_sql(column_type, out),
        (Value::Blob(bytes), &Type::BYTEA) => bytes.as_slice().to_sql(column_type, out),
        (Value::Blob(_), _) => Err(format!("bytes can't be stored in a {} column", column_type.name()).into()),
        _ => Err(format!("djox can't send {value:?} in binary").into()),
    }
}

/// A value as the text Postgres reads, or `None` for `NULL`.
fn text_of(value: &Value) -> Option<String> {
    let text = match value {
        Value::Null => return None,
        Value::Integer(number) => number.to_string(),
        // `{:?}` (Debug) writes a float so it reads back exactly, like Python's `repr()`.
        Value::Real(number) => format!("{number:?}"),
        Value::Text(text) => text.clone(),
        Value::Boolean(boolean) => boolean.to_string(),
        Value::Decimal(digits) => digits.clone(),
        Value::Date(date) => date.format("%Y-%m-%d").to_string(),
        Value::Time(time) => time.format("%H:%M:%S%.f").to_string(),
        Value::DateTime(moment) => moment.format("%Y-%m-%d %H:%M:%S%.f").to_string(),
        Value::DateTimeUtc(moment) => moment.format("%Y-%m-%d %H:%M:%S%.f+00:00").to_string(),
        Value::Duration(length) => match length.num_microseconds() {
            Some(microseconds) => format!("{microseconds} microseconds"),
            None => format!("{} milliseconds", length.num_milliseconds()),
        },
        Value::Uuid(uuid) => uuid.hyphenated().to_string(),
        // Never reached: bytes always go in binary (`is_sent_in_binary`).
        Value::Blob(_) => return None,
    };
    Some(text)
}

/// One column of a result row, read as a djox value.
///
/// Like [`Parameter`], a wrapper so we can implement the driver's `FromSql`.
pub struct Column(pub Value);

/// `'a` is the lifetime of the row's bytes: `FromSql` may borrow from them
/// (we always copy).
impl<'a> FromSql<'a> for Column {
    fn from_sql(column_type: &Type, raw: &'a [u8]) -> Result<Column, ConversionError> {
        let value = match *column_type {
            Type::BOOL => Value::Boolean(bool::from_sql(column_type, raw)?),
            Type::INT2 => Value::Integer(i64::from(i16::from_sql(column_type, raw)?)),
            Type::INT4 => Value::Integer(i64::from(i32::from_sql(column_type, raw)?)),
            Type::INT8 => Value::Integer(i64::from_sql(column_type, raw)?),
            Type::OID => Value::Integer(i64::from(u32::from_sql(column_type, raw)?)),
            Type::FLOAT4 => Value::Real(f64::from(f32::from_sql(column_type, raw)?)),
            Type::FLOAT8 => Value::Real(f64::from_sql(column_type, raw)?),
            Type::NUMERIC => Value::Decimal(numeric_text(raw)?),
            Type::TEXT | Type::VARCHAR | Type::BPCHAR | Type::NAME | Type::UNKNOWN | Type::JSON | Type::XML => {
                Value::Text(text_from_bytes(raw)?)
            }
            // `jsonb` in binary is a version byte (1) and then the JSON text.
            // Django reads JSON as text on purpose, and its `JSONField` parses it.
            Type::JSONB => Value::Text(text_from_bytes(raw.get(1..).unwrap_or(&[]))?),
            Type::BYTEA => Value::Blob(raw.to_vec()),
            Type::DATE => Value::Date(FromSql::from_sql(column_type, raw)?),
            Type::TIME => Value::Time(FromSql::from_sql(column_type, raw)?),
            Type::TIMESTAMP => Value::DateTime(FromSql::from_sql(column_type, raw)?),
            Type::TIMESTAMPTZ => Value::DateTimeUtc(FromSql::from_sql(column_type, raw)?),
            Type::INTERVAL => Value::Duration(interval_duration(raw)?),
            Type::UUID => Value::Uuid(FromSql::from_sql(column_type, raw)?),
            // Django reads IP addresses as text too.
            Type::INET => Value::Text(inet_text(raw, false)?),
            Type::CIDR => Value::Text(inet_text(raw, true)?),
            // What a function that returns nothing gives (`SELECT pg_sleep(1)`).
            Type::VOID => Value::Null,
            _ => read_other_type(column_type, raw)?,
        };
        Ok(Column(value))
    }

    /// A `NULL` column, whatever its type.
    fn from_sql_null(_column_type: &Type) -> Result<Column, ConversionError> {
        Ok(Column(Value::Null))
    }

    /// Any column can be read: an unknown type fails in `from_sql`, with its name.
    fn accepts(_column_type: &Type) -> bool {
        true
    }
}

/// Types without a fixed number in Postgres: an extension's `citext` and the
/// program's own `enum`s are text. Anything else isn't supported yet.
fn read_other_type(column_type: &Type, raw: &[u8]) -> Result<Value, ConversionError> {
    let is_text = column_type.name() == "citext" || matches!(column_type.kind(), Kind::Enum(_));
    if is_text {
        return Ok(Value::Text(text_from_bytes(raw)?));
    }
    Err(format!("djox can't read the Postgres type {} yet", column_type.name()).into())
}

fn text_from_bytes(raw: &[u8]) -> Result<String, ConversionError> {
    Ok(std::str::from_utf8(raw)?.to_string())
}

/// Reads a big-endian `u16` at `position`, like `struct.unpack_from(">H", ...)`.
fn read_u16(raw: &[u8], position: usize) -> Result<u16, ConversionError> {
    match raw.get(position..position + 2) {
        Some(two) => Ok(u16::from_be_bytes([two[0], two[1]])),
        None => Err("a Postgres value ended too early".into()),
    }
}

/// A `numeric` in binary, as the text Postgres would write for it.
///
/// Postgres stores a `numeric` in base 10000: a list of "digits" from 0 to
/// 9999, each worth four decimal digits. The header says how many there
/// are, where the decimal point goes (`weight`: the power of 10000 of the
/// first digit), the sign, and how many decimals to show (`display_scale`,
/// so `12.50` keeps its trailing zero, like Postgres and psycopg show it).
///
/// `123456.7` is the digits `[12, 3456, 7000]` with weight 1: `12`,
/// `3456`, then the point, then `7000`, shown with one decimal.
fn numeric_text(raw: &[u8]) -> Result<String, ConversionError> {
    let digit_count = usize::from(read_u16(raw, 0)?);
    // The weight can be negative (`0.0001` starts below the point), so it is signed.
    let weight = i64::from(read_u16(raw, 2)? as i16);
    let sign = read_u16(raw, 4)?;
    let display_scale = usize::from(read_u16(raw, 6)?);
    match sign {
        0xC000 => return Ok("NaN".to_string()),
        0xD000 => return Ok("Infinity".to_string()),
        0xF000 => return Ok("-Infinity".to_string()),
        _ => {}
    }
    let mut digits = Vec::with_capacity(digit_count);
    for index in 0..digit_count {
        digits.push(read_u16(raw, 8 + 2 * index)?);
    }
    // The digit at `position` (0 = the first one), or 0 outside the list.
    let digit_at = |position: i64| -> u16 {
        if position < 0 {
            return 0;
        }
        // `usize::try_from` fails for a position that can't be an index.
        usize::try_from(position)
            .ok()
            .and_then(|index| digits.get(index).copied())
            .unwrap_or(0)
    };

    let mut text = String::new();
    if sign == 0x4000 {
        text.push('-');
    }
    // The whole part: the digits up to `weight`, the first without leading zeros.
    if weight < 0 {
        text.push('0');
    } else {
        for position in 0..=weight {
            if position == 0 {
                text.push_str(&digit_at(position).to_string());
            } else {
                text.push_str(&format!("{:04}", digit_at(position)));
            }
        }
    }
    // The decimals: the digits after `weight`, four decimal digits each, cut to the display scale.
    if display_scale > 0 {
        let mut decimals = String::new();
        let mut position = weight + 1;
        while decimals.len() < display_scale {
            decimals.push_str(&format!("{:04}", digit_at(position)));
            position += 1;
        }
        decimals.truncate(display_scale);
        text.push('.');
        text.push_str(&decimals);
    }
    Ok(text)
}

/// An `interval` in binary, as a length of time.
///
/// Postgres keeps three parts: microseconds, days and months (a month has
/// no fixed length). Python's `timedelta` has no months, so, like psycopg,
/// a year counts 365 days and a month 30. Django's `DurationField` never
/// stores months, so its values are exact.
fn interval_duration(raw: &[u8]) -> Result<chrono::TimeDelta, ConversionError> {
    if raw.len() != 16 {
        return Err("a Postgres interval has 16 bytes".into());
    }
    let microseconds = i64::from_be_bytes(raw[0..8].try_into()?);
    let days = i64::from(i32::from_be_bytes(raw[8..12].try_into()?));
    let months = i64::from(i32::from_be_bytes(raw[12..16].try_into()?));
    let total_days = days + 365 * (months / 12) + 30 * (months % 12);
    Ok(chrono::TimeDelta::days(total_days) + chrono::TimeDelta::microseconds(microseconds))
}

/// An `inet` or `cidr` in binary, as the text Postgres writes for it:
/// `192.0.2.1`, or `192.0.2.0/24` with its network size (always shown for a `cidr`).
fn inet_text(raw: &[u8], is_cidr: bool) -> Result<String, ConversionError> {
    // family (2: IPv4, 3: IPv6), network bits, is-cidr flag, address length, address.
    let (Some(&family), Some(&bits)) = (raw.first(), raw.get(1)) else {
        return Err("a Postgres inet value ended too early".into());
    };
    let address = raw.get(4..).unwrap_or(&[]);
    let (text, all_bits) = match (family, address.len()) {
        (2, 4) => (
            Ipv4Addr::new(address[0], address[1], address[2], address[3]).to_string(),
            32,
        ),
        (3, 16) => {
            let octets: [u8; 16] = address.try_into()?;
            (Ipv6Addr::from(octets).to_string(), 128)
        }
        _ => return Err("djox can't read this Postgres inet value".into()),
    };
    if is_cidr || bits != all_bits {
        Ok(format!("{text}/{bits}"))
    } else {
        Ok(text)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// A `numeric` in binary, from its header and base-10000 digits.
    fn numeric(weight: i16, sign: u16, display_scale: u16, digits: &[u16]) -> Vec<u8> {
        let mut raw = Vec::new();
        raw.extend_from_slice(&(digits.len() as u16).to_be_bytes());
        raw.extend_from_slice(&weight.to_be_bytes());
        raw.extend_from_slice(&sign.to_be_bytes());
        raw.extend_from_slice(&display_scale.to_be_bytes());
        for digit in digits {
            raw.extend_from_slice(&digit.to_be_bytes());
        }
        raw
    }

    #[test]
    fn numerics_read_like_postgres_writes_them() {
        assert_eq!(numeric_text(&numeric(1, 0, 1, &[12, 3456, 7000])).unwrap(), "123456.7");
        assert_eq!(numeric_text(&numeric(0, 0x4000, 2, &[12, 5000])).unwrap(), "-12.50");
        assert_eq!(numeric_text(&numeric(-2, 0, 5, &[1000])).unwrap(), "0.00001");
        assert_eq!(numeric_text(&numeric(0, 0, 0, &[])).unwrap(), "0");
        assert_eq!(numeric_text(&numeric(0, 0, 3, &[])).unwrap(), "0.000");
        assert_eq!(numeric_text(&numeric(2, 0, 0, &[1])).unwrap(), "100000000");
        assert_eq!(numeric_text(&numeric(0, 0xC000, 0, &[])).unwrap(), "NaN");
    }

    fn interval(microseconds: i64, days: i32, months: i32) -> Vec<u8> {
        let mut raw = Vec::new();
        raw.extend_from_slice(&microseconds.to_be_bytes());
        raw.extend_from_slice(&days.to_be_bytes());
        raw.extend_from_slice(&months.to_be_bytes());
        raw
    }

    #[test]
    fn intervals_count_months_like_psycopg() {
        let length = interval_duration(&interval(1_500_000, 2, 0)).unwrap();
        assert_eq!(
            length,
            chrono::TimeDelta::days(2) + chrono::TimeDelta::microseconds(1_500_000)
        );
        assert_eq!(
            interval_duration(&interval(0, 0, 14)).unwrap(),
            chrono::TimeDelta::days(365 + 60)
        );
        assert_eq!(
            interval_duration(&interval(0, 0, -1)).unwrap(),
            chrono::TimeDelta::days(-30)
        );
    }

    #[test]
    fn ip_addresses_show_their_network_only_when_needed() {
        assert_eq!(inet_text(&[2, 32, 0, 4, 192, 0, 2, 1], false).unwrap(), "192.0.2.1");
        assert_eq!(inet_text(&[2, 24, 0, 4, 192, 0, 2, 0], false).unwrap(), "192.0.2.0/24");
        assert_eq!(inet_text(&[2, 32, 1, 4, 192, 0, 2, 1], true).unwrap(), "192.0.2.1/32");
        let mut ipv6 = vec![3, 128, 0, 16];
        ipv6.extend_from_slice(&"2001:db8::1".parse::<Ipv6Addr>().unwrap().octets());
        assert_eq!(inet_text(&ipv6, false).unwrap(), "2001:db8::1");
    }

    #[test]
    fn numbers_go_in_binary_only_when_they_fit() {
        assert!(is_sent_in_binary(&Value::Integer(5), &Type::INT4));
        assert!(!is_sent_in_binary(&Value::Integer(1 << 40), &Type::INT4));
        assert!(!is_sent_in_binary(&Value::Integer(5), &Type::NUMERIC));
        assert!(!is_sent_in_binary(&Value::Text("5".into()), &Type::INT4));
    }
}
