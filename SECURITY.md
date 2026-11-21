# Security Policy

## Scope

Adrenalift performs low-level GPU control. In **Safe mode** it talks to the GPU through the AMD display driver; in **Advanced mode** it additionally uses the PawnIO kernel driver with the RadeonSMU module for deeper telemetry and clock-limit access. Because of this, security issues in Adrenalift could potentially lead to privilege escalation, unsafe hardware access, or system instability, and are taken seriously.

This policy covers the official Adrenalift application distributed through the project's own channels. Issues in third-party components (e.g. the AMD driver, the PawnIO driver and its modules, or the operating system) should be reported to their respective vendors.

## Supported Versions

| Version | Supported |
|---------|-----------|
| Latest release | Yes |
| Older releases | No |

Always update to the latest release before reporting — older builds are not patched.

## Reporting a Vulnerability

**Do not open a public issue for security vulnerabilities.**

Instead, please report them privately using one of the following methods:

1. **GitHub Private Vulnerability Reporting:**
   Go to [Security Advisories](https://github.com/miklebel/adrenalift/security/advisories) and click "Report a vulnerability."

2. **Direct contact:**
   Reach the maintainer through the contact method listed on the [GitHub profile](https://github.com/miklebel).

### What to include

- Description of the vulnerability
- Steps to reproduce
- Affected version(s) and mode (Safe / Advanced)
- Potential impact (e.g., privilege escalation, unsafe hardware access)

### Response timeline

- **Acknowledgment:** within 72 hours
- **Initial assessment:** within 1 week
- **Fix or mitigation:** as soon as practical, coordinated with the reporter before public disclosure

## Responsible Disclosure

We ask that you give us reasonable time to address the issue before any public disclosure. We will credit reporters in the release notes unless they prefer to remain anonymous.
