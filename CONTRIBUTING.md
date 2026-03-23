# Contributing to Adrenalift

Thank you for your interest in contributing! This guide will help you get started.

## Getting Started

### Prerequisites

- **Python 3.10+**
- **pip**
- **Windows 10+** (64-bit) for runtime testing
- **AMD GPU** (RDNA4 primary, RDNA3 experimental)

### Development Setup

1. **Clone the repository:**

```bash
git clone https://github.com/miklebel/adrenalift.git
cd adrenalift
```

2. **Install dependencies:**

```bash
pip install -r requirements.txt
```

3. **Clone UPP (external dependency):**

```bash
cd deps
git clone https://github.com/sibradzic/upp.git
cd ..
```

4. **Place driver binaries** in `drivers/`:
   - `inpoutx64.dll`
   - `WinRing0x64.dll`
   - `WinRing0x64.sys`

### Building

```powershell
.\build.ps1
```

Or manually:

```bash
python -m PyInstaller --noconfirm build.spec
```

## How to Contribute

### Reporting Bugs

- Use the [Bug Report](https://github.com/miklebel/adrenalift/issues/new?template=bug_report.yml) issue template.
- Include your GPU model, driver version, Windows version, and Adrenalift version.
- Attach relevant log output if possible.

### Suggesting Features

- Use the [Feature Request](https://github.com/miklebel/adrenalift/issues/new?template=feature_request.yml) issue template.
- Describe the use case, not just the solution.

### Pull Requests

1. **Fork** the repository and create a branch from `main`.
2. Keep changes focused — one feature or fix per PR.
3. Test your changes on real hardware if possible, or clearly state what was and wasn't tested.
4. Update the README if your change affects usage or build instructions.
5. Open a pull request against `main` using the PR template.

### What We're Looking For

- **RDNA3 testing and fixes** — RDNA3 support exists but is untested.
- **Bug reports with logs** — especially driver crashes, BSODs, or incorrect clock behavior.
- **Documentation improvements** — clearer explanations, typo fixes, additional examples.
- **New GPU family support** — extending PowerPlay table parsing to other architectures.
- **Safety improvements** — better validation, bounds checking, error handling.

## Code Style

- No strict linter is enforced yet, but keep code readable and consistent with the existing style.
- Avoid adding comments that merely narrate what the code does.
- Meaningful variable names over short abbreviations.

## Safety Notice

This project interacts with hardware at a low level. If you are contributing code that writes to physical memory, SMU mailboxes, or modifies driver state:

- **Validate all offsets and sizes** before writing.
- **Bounds-check** any user-supplied values.
- **Fail safely** — prefer doing nothing over corrupting memory.
- **Document assumptions** about hardware behavior.

## License

By contributing, you agree that your contributions will be licensed under the [GNU General Public License v3.0](LICENSE).
