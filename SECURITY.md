# Security Policy

## Scope

Adrenalift writes directly to physical memory and communicates with the GPU's System Management Unit (SMU). Security issues in this project could lead to privilege escalation, arbitrary memory writes, or system instability.

## Supported Versions

| Version | Supported |
|---------|-----------|
| Latest release | Yes |
| Older releases | No |

## Reporting a Vulnerability

**Do not open a public issue for security vulnerabilities.**

Instead, please report them privately using one of the following methods:

1. **GitHub Private Vulnerability Reporting:**
   Go to [Security Advisories](https://github.com/miklebel/adrenalift/security/advisories) and click "Report a vulnerability."

2. **Email:**
   Contact the maintainer directly at the email listed on the [GitHub profile](https://github.com/miklebel).

### What to include

- Description of the vulnerability
- Steps to reproduce
- Affected version(s)
- Potential impact (e.g., arbitrary memory write, privilege escalation)

### Response timeline

- **Acknowledgment:** within 72 hours
- **Initial assessment:** within 1 week
- **Fix or mitigation:** as soon as practical, coordinated with the reporter before public disclosure

## Responsible Disclosure

We ask that you give us reasonable time to address the issue before any public disclosure. We will credit reporters in the release notes unless they prefer to remain anonymous.
