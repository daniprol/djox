//! TLS: encrypting the connection to the server, and checking who the server is.
//!
//! Postgres asks the same two questions as libpq, through the same settings
//! (Django passes them in `OPTIONS`):
//!
//! | `sslmode`     | Encrypted?                  | Server's certificate checked?                    |
//! |---------------|-----------------------------|--------------------------------------------------|
//! | `disable`     | no                          | -                                                |
//! | `allow`       | if the server offers it (*) | no                                               |
//! | `prefer`      | if the server offers it     | no (libpq's default)                             |
//! | `require`     | yes                         | no, unless `sslrootcert` is given: then like `verify-ca` |
//! | `verify-ca`   | yes                         | signed by a trusted authority                    |
//! | `verify-full` | yes                         | signed by a trusted authority, and made out to the host name |
//!
//! (*) libpq tries a plain connection first with `allow`; djox tries TLS
//! first, like `prefer`. Both end up connected to the same servers.
//!
//! Without a check, an attacker on the network could pretend to be the
//! server: the data is encrypted, but maybe to the wrong party. So use
//! `verify-full` across networks you don't control.
//!
//! The trusted authorities (`sslrootcert`) are a PEM file of certificates,
//! or `system` for the ones the operating system trusts. djox uses the
//! system's when `sslrootcert` isn't given (libpq would look for
//! `~/.postgresql/root.crt` instead). A client certificate (`sslcert` and
//! `sslkey`, PEM files) proves who djox is to a server that asks.
//!
//! The encryption is rustls, written in Rust: the wheels need no OpenSSL.

// `Arc`: a shared pointer, like a Python reference several owners can hold.
use std::sync::Arc;

use djox_core::{Error, Result};
use rustls::client::WebPkiServerVerifier;
use rustls::client::danger::{HandshakeSignatureValid, ServerCertVerified, ServerCertVerifier};
use rustls::crypto::CryptoProvider;
use rustls::pki_types::pem::PemObject;
use rustls::pki_types::{CertificateDer, PrivateKeyDer, ServerName, UnixTime};
use rustls::{CertificateError, ClientConfig, DigitallySignedStruct, RootCertStore, SignatureScheme};
use tokio_postgres::config::SslMode;
use tokio_postgres_rustls::MakeRustlsConnect;

/// How much TLS to use: libpq's `sslmode` (see the table at the top).
// `#[derive(...)]` asks Rust to write the code that copies, compares and
// prints the value, like a `@dataclass` does in Python.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TlsMode {
    Disable,
    Allow,
    Prefer,
    Require,
    VerifyCa,
    VerifyFull,
}

impl TlsMode {
    /// Reads libpq's spelling: `"disable"`, `"verify-full"`...
    pub fn from_name(name: &str) -> Result<TlsMode> {
        match name {
            "disable" => Ok(TlsMode::Disable),
            "allow" => Ok(TlsMode::Allow),
            "prefer" => Ok(TlsMode::Prefer),
            "require" => Ok(TlsMode::Require),
            "verify-ca" => Ok(TlsMode::VerifyCa),
            "verify-full" => Ok(TlsMode::VerifyFull),
            _ => Err(Error::Settings(format!(
                "{name:?} is not an sslmode: use disable, allow, prefer, require, verify-ca or verify-full"
            ))),
        }
    }

    /// The mode the driver understands. It knows only whether to try TLS
    /// (`Prefer`) or insist on it (`Require`); checking the certificate is
    /// the job of the verifier made by `tls_connector()`.
    pub fn driver_mode(self) -> SslMode {
        match self {
            TlsMode::Disable => SslMode::Disable,
            TlsMode::Allow | TlsMode::Prefer => SslMode::Prefer,
            TlsMode::Require | TlsMode::VerifyCa | TlsMode::VerifyFull => SslMode::Require,
        }
    }

    /// The mode the driver read from the connection text, which knows
    /// only `disable`, `prefer` (the default) and `require`.
    pub fn from_driver_mode(mode: SslMode) -> TlsMode {
        match mode {
            SslMode::Disable => TlsMode::Disable,
            SslMode::Require => TlsMode::Require,
            // `_`: `Prefer`, and any mode a later driver version adds.
            _ => TlsMode::Prefer,
        }
    }
}

/// libpq's TLS settings, as Django's `OPTIONS` names them.
#[derive(Debug, Clone, Default)]
pub struct TlsSettings {
    /// `sslmode`. `None` (`Option`: a value or `None`) keeps the mode the
    /// connection text gives, or `prefer`.
    pub mode: Option<TlsMode>,
    /// `sslrootcert`: a PEM file of trusted authorities, or `"system"`.
    pub root_certificates: Option<String>,
    /// `sslcert`: djox's own certificate, a PEM file.
    pub client_certificate: Option<String>,
    /// `sslkey`: the private key of `client_certificate`, a PEM file.
    pub client_key: Option<String>,
}

/// What the driver uses to open TLS connections, set up for `mode`.
///
/// It reads the certificate files now, so a missing or broken file fails
/// when the database opens, not at the first query.
pub fn tls_connector(settings: &TlsSettings, mode: TlsMode) -> Result<MakeRustlsConnect> {
    // The cryptography rustls runs on.
    let provider = Arc::new(rustls::crypto::ring::default_provider());

    let checks_authority = match mode {
        TlsMode::VerifyCa | TlsMode::VerifyFull => true,
        // libpq's rule: `require` with a root certificate given checks it.
        TlsMode::Require => settings.root_certificates.is_some(),
        TlsMode::Disable | TlsMode::Allow | TlsMode::Prefer => false,
    };
    // `Arc<dyn ServerCertVerifier>`: any type that can verify, like a
    // variable typed with a `Protocol` in Python.
    let verifier: Arc<dyn ServerCertVerifier> = if checks_authority {
        let roots = trusted_authorities(settings.root_certificates.as_deref())?;
        let checks_everything = WebPkiServerVerifier::builder_with_provider(Arc::new(roots), provider.clone())
            .build()
            .map_err(|error| Error::Settings(format!("can't use the trusted certificates: {error}")))?;
        if mode == TlsMode::VerifyFull {
            checks_everything
        } else {
            Arc::new(AuthorityOnly { checks_everything })
        }
    } else {
        Arc::new(AnyCertificate {
            provider: provider.clone(),
        })
    };

    // A builder, like `ClientConfig(protocols=..., verifier=..., client_auth=...)` in steps.
    let builder = ClientConfig::builder_with_provider(provider)
        .with_safe_default_protocol_versions()
        .map_err(|error| Error::Settings(format!("can't set up TLS: {error}")))?
        // "dangerous": rustls's word for "I check certificates my own way".
        .dangerous()
        .with_custom_certificate_verifier(verifier);
    let config = match (&settings.client_certificate, &settings.client_key) {
        (Some(certificate_file), Some(key_file)) => {
            let certificates = read_certificates(certificate_file)?;
            let key = PrivateKeyDer::from_pem_file(key_file)
                .map_err(|error| Error::Settings(format!("can't read the sslkey file {key_file:?}: {error}")))?;
            builder
                .with_client_auth_cert(certificates, key)
                .map_err(|error| Error::Settings(format!("can't use sslcert and sslkey: {error}")))?
        }
        (None, None) => builder.with_no_client_auth(),
        _ => {
            return Err(Error::Settings(
                "sslcert and sslkey go together: give both, or neither".to_string(),
            ));
        }
    };
    Ok(MakeRustlsConnect::new(config))
}

/// The authorities whose signature on the server's certificate we trust.
fn trusted_authorities(root_certificates: Option<&str>) -> Result<RootCertStore> {
    let mut roots = RootCertStore::empty();
    match root_certificates {
        None | Some("system") => {
            let found = rustls_native_certs::load_native_certs();
            // Unreadable certificates in the system's store are skipped, as libpq does.
            roots.add_parsable_certificates(found.certs);
            if roots.is_empty() {
                return Err(Error::Settings(
                    "found no trusted certificates on this system: give sslrootcert a PEM file".to_string(),
                ));
            }
        }
        Some(file) => {
            for certificate in read_certificates(file)? {
                roots
                    .add(certificate)
                    .map_err(|error| Error::Settings(format!("can't use a certificate in {file:?}: {error}")))?;
            }
        }
    }
    Ok(roots)
}

/// Every certificate in a PEM file.
fn read_certificates(file: &str) -> Result<Vec<CertificateDer<'static>>> {
    let unreadable = |error: rustls::pki_types::pem::Error| Error::Settings(format!("can't read {file:?}: {error}"));
    let mut certificates = Vec::new();
    for certificate in CertificateDer::pem_file_iter(file).map_err(unreadable)? {
        certificates.push(certificate.map_err(unreadable)?);
    }
    if certificates.is_empty() {
        return Err(Error::Settings(format!("{file:?} holds no PEM certificate")));
    }
    Ok(certificates)
}

/// `verify-ca`: the certificate must be signed by a trusted authority, but
/// may be made out to another name than the host's.
// `#[derive(Debug)]`: rustls wants to be able to print its verifier.
#[derive(Debug)]
struct AuthorityOnly {
    checks_everything: Arc<WebPkiServerVerifier>,
}

// `impl Trait for Type`: the methods that make `AuthorityOnly` a
// `ServerCertVerifier`, like implementing a Python `Protocol`.
impl ServerCertVerifier for AuthorityOnly {
    fn verify_server_cert(
        &self,
        certificate: &CertificateDer<'_>,
        intermediates: &[CertificateDer<'_>],
        server_name: &ServerName<'_>,
        ocsp_response: &[u8],
        now: UnixTime,
    ) -> std::result::Result<ServerCertVerified, rustls::Error> {
        let checked =
            self.checks_everything
                .verify_server_cert(certificate, intermediates, server_name, ocsp_response, now);
        match checked {
            // The name is checked last, once the signatures are good: a wrong
            // name is the only thing `verify-ca` lets through.
            Err(rustls::Error::InvalidCertificate(CertificateError::NotValidForName))
            | Err(rustls::Error::InvalidCertificate(CertificateError::NotValidForNameContext { .. })) => {
                Ok(ServerCertVerified::assertion())
            }
            other => other,
        }
    }

    fn verify_tls12_signature(
        &self,
        message: &[u8],
        certificate: &CertificateDer<'_>,
        signature: &DigitallySignedStruct,
    ) -> std::result::Result<HandshakeSignatureValid, rustls::Error> {
        self.checks_everything
            .verify_tls12_signature(message, certificate, signature)
    }

    fn verify_tls13_signature(
        &self,
        message: &[u8],
        certificate: &CertificateDer<'_>,
        signature: &DigitallySignedStruct,
    ) -> std::result::Result<HandshakeSignatureValid, rustls::Error> {
        self.checks_everything
            .verify_tls13_signature(message, certificate, signature)
    }

    fn supported_verify_schemes(&self) -> Vec<SignatureScheme> {
        self.checks_everything.supported_verify_schemes()
    }
}

/// `prefer` and `require`: any certificate will do, as with libpq. The
/// handshake's signatures are still checked, so the connection is really
/// encrypted to whoever holds the certificate's key.
#[derive(Debug)]
struct AnyCertificate {
    provider: Arc<CryptoProvider>,
}

impl ServerCertVerifier for AnyCertificate {
    fn verify_server_cert(
        &self,
        _certificate: &CertificateDer<'_>,
        _intermediates: &[CertificateDer<'_>],
        _server_name: &ServerName<'_>,
        _ocsp_response: &[u8],
        _now: UnixTime,
    ) -> std::result::Result<ServerCertVerified, rustls::Error> {
        Ok(ServerCertVerified::assertion())
    }

    fn verify_tls12_signature(
        &self,
        message: &[u8],
        certificate: &CertificateDer<'_>,
        signature: &DigitallySignedStruct,
    ) -> std::result::Result<HandshakeSignatureValid, rustls::Error> {
        let algorithms = &self.provider.signature_verification_algorithms;
        rustls::crypto::verify_tls12_signature(message, certificate, signature, algorithms)
    }

    fn verify_tls13_signature(
        &self,
        message: &[u8],
        certificate: &CertificateDer<'_>,
        signature: &DigitallySignedStruct,
    ) -> std::result::Result<HandshakeSignatureValid, rustls::Error> {
        let algorithms = &self.provider.signature_verification_algorithms;
        rustls::crypto::verify_tls13_signature(message, certificate, signature, algorithms)
    }

    fn supported_verify_schemes(&self) -> Vec<SignatureScheme> {
        self.provider.signature_verification_algorithms.supported_schemes()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn reads_libpqs_mode_names() {
        assert_eq!(TlsMode::from_name("verify-full").unwrap(), TlsMode::VerifyFull);
        assert_eq!(TlsMode::from_name("allow").unwrap().driver_mode(), SslMode::Prefer);
        assert_eq!(TlsMode::from_name("verify-ca").unwrap().driver_mode(), SslMode::Require);
        assert!(matches!(TlsMode::from_name("on"), Err(Error::Settings(_))));
    }

    #[test]
    fn refuses_certificate_files_it_cant_use() {
        let missing = TlsSettings {
            root_certificates: Some("/no/such/root.crt".to_string()),
            ..TlsSettings::default()
        };
        assert!(matches!(
            tls_connector(&missing, TlsMode::VerifyFull),
            Err(Error::Settings(_))
        ));
        // Not checked, so not read: `prefer` never looks at the file.
        assert!(tls_connector(&missing, TlsMode::Prefer).is_ok());

        let half = TlsSettings {
            client_certificate: Some("client.crt".to_string()),
            ..TlsSettings::default()
        };
        assert!(matches!(
            tls_connector(&half, TlsMode::Require),
            Err(Error::Settings(_))
        ));
    }
}
