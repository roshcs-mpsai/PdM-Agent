# Extra root certificates for the image build

On a company network that inspects HTTPS, a proxy re-signs every connection with the company's
own root certificate. The Mac trusts it; a fresh container does not, so `docker compose build`
stops at `pip install` with `CERTIFICATE_VERIFY_FAILED`.

Put that root certificate here as a PEM file ending in `.crt`, then build again. docker/Dockerfile
adds every `*.crt` in this folder to the image's trusted bundle. The files are git-ignored: they are
public certificates, not secrets, but they belong to one network, not to the project.

On a Mac, the IT-installed roots are in the System keychain:

```bash
security find-certificate -a -p /Library/Keychains/System.keychain > docker/ca/macos-system.crt
docker compose build
```

That exports every certificate in the System keychain, which is more than needed and does no harm.
On a network without inspection, leave the folder empty.
