#!/usr/bin/env python3
"""Repeatable privacy gate: fail the build if owner-identifying data, host
paths, key material, or Pine Script source would ship in the public source
tree. No Pine is ever allowed in this repo -- see pine_findings() below.

Run before `build/build.sh` archives and exports a release:

    python3 tools/privacy_check.py [--root REPO_DIR]

Exit code is 0 (clean) or 1 (one or more findings; printed to stderr as
`path:line: reason: snippet`).

`--history` additionally scans the *entire* git history of every ref
(`git log --all -p --text`) for the same patterns, and checks every commit
author/committer and every annotated tag's tagger against the one approved
public identity (`Decrypt Labs <noreply@decryptlabs.io>`), and requires every
author, committer and tagger date to carry a +0000 UTC offset (a local
offset such as +0200 reveals the maintainer's timezone). It also scans every
tag name, every annotated tag message and every file path that ever existed
in history against the same patterns. This is slow and
is meant for the PUBLIC repo only -- a freshly made single-commit history
(see `tools/make_public_repo.sh`), never a development repository whose
history legitimately carries individual contributors' commits.

Design notes (read before adding patterns):

- This file is part of the PUBLIC repo, so its built-in pattern list stays
  generic: structural secret/PII shapes (private key headers, cloud API key
  prefixes, non-placeholder home-directory/macOS host paths, email
  addresses outside a short reserved-for-examples allowlist). It never
  hardcodes anyone's real name, host, or account value. A bare private-IP
  (RFC1918) regex was deliberately left out of the built-ins: this repo's
  own network-isolation policy and bridge tests legitimately define/use
  10.x/172.16.x/192.168.x addresses as generic ranges and fixture values,
  so a blanket ban is noise, not signal, here. A real, specific leaked
  address belongs in DECRYPT_PRIVATE_PATTERNS instead (see below).
- Real, specific values the repo owner wants to also ban (their own name,
  a real hostname, a real IP, an internal domain, ...) do not belong in
  this script or anywhere else in the repo. Set DECRYPT_PRIVATE_PATTERNS to
  the path of a private file (kept OUTSIDE this repo, one regex per line,
  '#' comments and blank lines ignored) with those additions. This script
  reads that file's path from the environment; it never stores or prints
  the file's own location in build output beyond "N extra pattern(s)
  loaded". Because real names/codenames/account numbers are detected only
  via that external file, its own content and last-reviewed date are not
  tracked here: confirm DECRYPT_PRIVATE_PATTERNS is current (covers any
  real name, host, account or codename introduced since the last release)
  before each release export, not just before the first one.
- Scans exactly the files `git ls-files` reports (i.e. exactly what
  `git archive`/the release build would ship) so build caches, venvs and
  local-only files never cause false positives or false confidence. Every
  tracked file is scanned regardless of its extension -- there is no
  suffix allowlist to fall through -- and a file is skipped only if its
  bytes do not decode as UTF-8 text (an actual binary).
- Every pattern match on a line is reported (via `finditer`, not just the
  first `search` hit), so two findings on one line are both printed.
- A short, explicit EXEMPT_FILES list below exists only for files that
  legitimately *contain* one of these patterns as data, not as a leak:
  third-party license/notice attributions (real upstream contributor
  emails we must keep, see NOTICES.md/LICENSE policy) and this repo's own
  existing negative tests / leak-scanners, whose job is to assert a
  pattern is ABSENT from something else and so must contain the pattern
  text itself to test for it. Exemptions are by exact relative path, each
  with a one-line reason -- never a blanket directory skip. An exemption
  only skips the built-in generic rules: the owner's DECRYPT_PRIVATE_PATTERNS
  still run on exempt files (tree and --history), since no file has a
  legitimate reason to carry the owner's real values.
- A JSON contract may pin private names by SHA-256 only (`input_digests`).
  digest_name_findings() fails any tracked line, exempt files included,
  carrying a word or quoted string that hashes to one of those digests.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

# Exact relative paths (from the repo root) that are allowed to contain a
# matched pattern as data rather than a leak. Keep this list short and
# reviewed -- it is not a way to silence a real finding elsewhere.
EXEMPT_FILES = {
    # Required third-party copyright/contact lines (NOTICES.md policy: keep
    # attributions). Reviewed upstream attributions; not owner data.
    'candidate/app-source/app/ui/licenses/UI-THIRD-PARTY-NOTICES.txt':
        'third-party license attributions, not owner data',
    'candidate/app-source/customer-docs/UI-THIRD-PARTY-NOTICES.txt':
        'third-party license attributions, not owner data',
    # This repo's own pre-bundle secret scanner: it contains the PEM/AWS-key
    # regex text as source, which trivially matches itself.
    'candidate/app-source/lab/build_source_bundle.py':
        'defines the private-key/AWS-key leak-detection regex itself',
    # Negative tests: they assert these substrings never appear in some
    # OTHER generated output, so the pattern text must appear here as the
    # literal they search for.
    'candidate/app-source/tests/test_ict_assistant.py':
        'asserts host paths/emails/IPs are absent from assistant output',
    'candidate/app-source/installer/test_customer_ingress.py':
        'asserts rejected hostname forms (a.local, a.test, a.invalid, ...) are refused',
    # This tool and its own test fixtures necessarily contain the pattern
    # text (as regexes / as synthetic positive-match samples).
    'tools/privacy_check.py': 'defines the patterns',
    'tools/test_privacy_check.py': 'synthetic positive/negative fixtures',
    'tools/test_make_public_repo.py':
        'synthetic fixture proving a real secret in a committed tag fails --history',
    # Defines the same path/secret-shape patterns this file does, for
    # diagnostics-bundle redaction.
    'candidate/tools/diagnostics.py': 'defines the redaction patterns',
    'candidate/tools/test_diagnostics.py': 'synthetic positive-match redaction fixtures',
}

# Email domains reserved for documentation/examples (RFC 2606 + RFC 6761),
# never real mailboxes.
SAFE_EMAIL_DOMAINS = re.compile(
    r'@([\w-]+\.)*(example\.(com|net|org|invalid)|test|invalid|localhost)$', re.I)

# Public role addresses of the project itself (release signer identity, commit
# author). Exact matches only: any other address on the domain is still flagged.
PUBLIC_ROLE_ADDRESSES = {'release@decryptlabs.io', 'noreply@decryptlabs.io', 'conduct@decryptlabs.io'}

# The public source repository. Its URL necessarily carries the hosting
# account's handle, so this exact URL (case-sensitive, nothing else) is masked
# before patterns run; any other use of the handle is still flagged.
# The project is published under this account.
PUBLIC_REPO_URL = 'https://github.com/Dropkingeth/decrypt-os'

# The one identity public commits/tags are allowed to carry (--history mode).
APPROVED_PUBLIC_IDENTITY = 'Decrypt Labs <noreply@decryptlabs.io>'

# systemd instance-unit syntax ("name@instance.service") is a very common,
# entirely unrelated use of '@' in this repo (unit names, template specs);
# never treat it as an email candidate.
SYSTEMD_UNIT_SUFFIX = re.compile(
    r'\.(service|timer|socket|target|mount|path|slice|scope)$', re.I)

# Generic placeholder local-parts used throughout this repo's own test
# fixtures and URL-userinfo negative tests (e.g. 'https://user:pass@host'
# asserting embedded credentials are refused) -- not anyone's mailbox.
# Deliberately excludes 'admin'/'root': those are the two local-parts most
# likely to be a REAL operational mailbox (admin@realcompany.com,
# root@customer-server.example), so they get no blanket pass here -- only
# the same safe-domain/public-role-address exemptions as any other address.
PLACEHOLDER_LOCAL_PARTS = {
    'user', 'pass', 'test', 'fixture', 'fake', 'token', 'key', 'mock', 'demo',
}

# Fake-value markers this repo's own test fixtures consistently use for
# synthetic secrets (e.g. 'fixture-login-password-ONLY',
# 'never-log-this-fixture-secret', 'FAKE-HTTP-UI-PROVIDER-PASSWORD'). Every
# word alternative is \b-anchored (not just 'test') so a real secret whose
# value merely happens to *contain* one of these words mid-run (e.g. a
# base64-ish blob with "...9fakeM3..." in it, no delimiter on either side)
# is not wrongly treated as a placeholder -- only a genuinely delimited
# whole word (as in the repo's own fixtures above) is exempt.
FAKE_VALUE_MARKERS = re.compile(
    r'(?i)\b(?:fixture|fake|dummy|placeholder|not[_-]a[_-]real|change[_-]?me|test)\b'
    # obviously-sequential filler (0123456789, 1234567890, abcdef, ...): no
    # real secret generator produces this, only a readable test placeholder.
    r'|0123456789|1234567890|abcdef')

PATTERNS = {
    'private key material': re.compile(
        r'-----BEGIN (?:RSA |EC |DSA |OPENSSH |ENCRYPTED )?PRIVATE KEY-----'
        r'|-----BEGIN PGP PRIVATE KEY BLOCK-----'),
    'AWS access key id': re.compile(r'\bAKIA[0-9A-Z]{16}\b'),
    # gh[p/o/u/s/r]_: personal-access/OAuth/user-to-server/server-to-server/
    # refresh tokens; github_pat_: fine-grained personal access tokens.
    'GitHub token': re.compile(r'\bgh[pousr]_[A-Za-z0-9]{30,}\b'),
    'GitHub fine-grained token': re.compile(r'\bgithub_pat_[A-Za-z0-9_]{30,}\b'),
    'Slack token': re.compile(r'\bxox[baprs]-[A-Za-z0-9-]{10,}\b'),
    'generic API-style secret (sk-.../sk_...)': re.compile(r'\bsk[-_][A-Za-z0-9_-]{20,}\b'),
    'JWT (eyJ...)': re.compile(r'\beyJ[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}\b'),
    'Bearer token': re.compile(r'(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{20,}\b'),
    'Telegram bot token': re.compile(r'\b\d{8,10}:[A-Za-z0-9_-]{35}\b'),
    'Discord token': re.compile(r'\b[MNO][A-Za-z0-9_-]{23,25}\.[A-Za-z0-9_-]{6}\.[A-Za-z0-9_-]{27,}\b'),
    'secret/token/key/password literal': re.compile(
        r'(?i)\b(api[_-]?key|secret|token|password|passwd)["\']?\s*[:=]\s*'
        r'["\'](?P<value>[A-Za-z0-9+/_=-]{20,})["\']'),
    # No trailing-slash requirement: a bare path at end-of-line or followed
    # by punctuation other than '/' (e.g. "/Users/someuser", "/Users/someuser.")
    # must still match, not just "/Users/someuser/".
    'macOS host path (/Users/<name>)': re.compile(r'/Users/[^/\s"\'<>]+'),
    # Tailscale CGNAT range 100.64.0.0/10 (100.64.0.0-100.127.255.255). The
    # trailing (?!/) lets a CIDR definition like "100.64.0.0/10" describing
    # the *range itself* (this repo's own network policy) through, while
    # still catching a specific bare address inside that range.
    'Tailscale CGNAT address (100.64.0.0/10)': re.compile(
        r'\b100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d{1,3}\.\d{1,3}\b(?!/)'),
    'Tailscale hostname (*.ts.net)': re.compile(r'\b[\w-]+(?:\.[\w-]+)*\.ts\.net\b'),
    '*.local hostname': re.compile(r'\b[\w-]+\.local\b'),
    'email address outside the safe example domains': re.compile(
        r'\b[\w.+-]+@([\w-]+\.)+[A-Za-z]{2,}\b'),
    # A saved TradingView chart layout URL names one specific account's
    # layout. Only the synthetic fixture ids below may appear; the bare
    # 'https://www.tradingview.com/chart/' launcher URL has no id and passes.
    'TradingView chart layout link': re.compile(
        r'tradingview\.com/chart/(?P<chart_id>[A-Za-z0-9_-]+)', re.I),
}

# Synthetic chart layout ids used by this repo's own test fixtures.
FIXTURE_CHART_IDS = {'Test1234', 'AbCd1234', 'AbCd5678'}

# /home/<name> is legitimate when <name> is a documented generic
# placeholder (customer install docs, bridge example configs, short test
# variables, etc.), so it gets its own check with an allowlist instead of
# a blanket ban.
# Same no-trailing-slash fix as the macOS pattern above: a bare path at
# end-of-line or followed by non-'/' punctuation must still match.
HOME_PATH = re.compile(r'/home/([^/\s"\'<>]+)')
GENERIC_HOME_NAMES = {'customer', 'trader', 'decrypt', 'user', 'account', 'test', 'demo'}

# No Pine Script source is ever allowed in this repo (project rule: never
# copy .pine files, Pine source, or large Pine excerpts). A .pine
# extension always fails, checked by path alone in pine_findings() below.
# For other files, Pine's version-pragma comment is unique to Pine Script
# and essentially never appears elsewhere, so it is a self-sufficient
# signal. A strategy()/indicator()/library() declaration is not unique by
# itself (another language could plausibly define a function with that
# name), so it is only treated as Pine when paired with a Pine-only
# namespaced call (ta.*, input.*, ...) elsewhere in the same file -- the
# repo has zero matches for this combination today (checked against the
# full tree), so it adds signal without adding noise.
PINE_VERSION_HEADER = re.compile(r'^\s*//@version\s*=\s*\d+', re.MULTILINE)
PINE_DECLARATION = re.compile(r'\b(strategy|indicator|library)\s*\(\s*["\']')
PINE_NAMESPACE_USAGE = re.compile(
    r'\b(ta|input|strategy|request|ticker|syminfo|alertcondition)\.[a-zA-Z_]\w*\s*\(')


def pine_findings(path, text):
    """Pine Script source detection, independent of PATTERNS: a .pine
    extension fails outright (checked by the caller before this even needs
    file content -- see scan_file); otherwise a //@version= header, or a
    Pine declaration call paired with Pine namespace usage anywhere in the
    same file, fails. Returns at most one finding per file (enough to fail
    the gate; the exact line is a pointer, not an exhaustive scan)."""
    if path.suffix == '.pine':
        yield 1, 'Pine source file (.pine extension)', path.name
        return
    version_match = PINE_VERSION_HEADER.search(text)
    if version_match:
        line = text.count('\n', 0, version_match.start()) + 1
        yield line, 'Pine Script source (//@version= header)', version_match.group(0).strip()
        return
    declaration_match = PINE_DECLARATION.search(text)
    if declaration_match and PINE_NAMESPACE_USAGE.search(text):
        line = text.count('\n', 0, declaration_match.start()) + 1
        yield (line, 'Pine Script source (strategy/indicator/library declaration with Pine namespace usage)',
               declaration_match.group(0).strip())


def tracked_files(root):
    """Exactly what `git archive HEAD` would ship -- matches the real build input."""
    out = subprocess.run(['git', '-C', str(root), 'ls-files'],
                          capture_output=True, text=True, check=True)
    return [root / line for line in out.stdout.splitlines() if line]


def load_extra_patterns():
    """DECRYPT_PRIVATE_PATTERNS: path to a file OUTSIDE this repo, one
    regex per line, '#' comments and blank lines ignored. Never required;
    missing/unset means no extra patterns, not an error."""
    path = os.environ.get('DECRYPT_PRIVATE_PATTERNS')
    if not path:
        return {}
    try:
        lines = Path(path).expanduser().read_text().splitlines()
    except OSError as exc:
        print(f'warning: DECRYPT_PRIVATE_PATTERNS set but unreadable ({exc}); '
              'continuing with built-in patterns only', file=sys.stderr)
        return {}
    extra = {}
    for i, line in enumerate(lines, 1):
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        try:
            extra[f'extra pattern (line {i})'] = re.compile(line)
        except re.error as exc:
            print(f'warning: DECRYPT_PRIVATE_PATTERNS line {i} is not a valid '
                  f'regex ({exc}); skipped', file=sys.stderr)
    if extra:
        print(f'privacy_check: {len(extra)} extra pattern(s) loaded from '
              'DECRYPT_PRIVATE_PATTERNS', file=sys.stderr)
    return extra


def is_exempt_match(reason, match):
    """True if this specific match is data, not a leak -- independent of
    which file it came from (used for both per-file and --history scans).
    `match` is the regex Match object, not just its matched text, so a
    reason can check a specific capture group (e.g. just the value of a
    key=value secret literal) instead of the whole matched span."""
    matched_text = match.group(0)
    if reason == 'email address outside the safe example domains':
        local, _, domain = matched_text.partition('@')
        return (bool(SAFE_EMAIL_DOMAINS.search(matched_text))
                or matched_text.lower() in PUBLIC_ROLE_ADDRESSES
                or bool(SYSTEMD_UNIT_SUFFIX.search(domain))
                or local.lower() in PLACEHOLDER_LOCAL_PARTS)
    if reason == 'secret/token/key/password literal':
        # Check only the matched *value*, not the key name beside it -- a
        # real secret whose value happens to contain a word like "test" as
        # a substring must not be exempted just because the key name (or
        # value) elsewhere in the span would otherwise trip the marker.
        value = match.group('value') if 'value' in match.groupdict() else matched_text
        return bool(FAKE_VALUE_MARKERS.search(value))
    if reason == 'TradingView chart layout link':
        return matched_text.rsplit('/', 1)[1] in FIXTURE_CHART_IDS
    return False


def scan_lines(lines, patterns, generic=True):
    """Yield (lineno, reason, snippet) for every non-exempt match, over any
    1-indexed iterable of text lines (a file's lines, or a diff's lines).
    generic=False skips the built-in /home/<name> check (exempt files get
    only the owner's extra patterns)."""
    for lineno, line in enumerate(lines, 1):
        line = line.replace(PUBLIC_REPO_URL, '<public-repo-url>')
        for reason, pattern in patterns.items():
            for match in pattern.finditer(line):
                if is_exempt_match(reason, match):
                    continue
                yield lineno, reason, match.group(0)
        if not generic:
            continue
        for home in HOME_PATH.finditer(line):
            name = home.group(1).lower()
            if name not in GENERIC_HOME_NAMES and len(name) > 2:
                yield lineno, 'non-placeholder /home/<name> path', home.group(0)


def scan_file(path, patterns):
    if path.suffix == '.pine':
        # Checked by extension alone, before any decode attempt: an
        # undecodable/binary-looking .pine file must still fail, not be
        # waved through as "not a text leak".
        return list(pine_findings(path, ''))
    try:
        text = path.read_text(encoding='utf-8')
    except (UnicodeDecodeError, OSError):
        return []  # binary or unreadable: not a text leak
    findings = list(scan_lines(text.splitlines(), patterns))
    findings.extend(pine_findings(path, text))
    return sorted(findings, key=lambda item: item[0])


HEX_DIGEST = re.compile(r'\A[0-9a-f]{64}\Z')
DIGEST_NAME_CANDIDATES = (re.compile(r'[A-Za-z0-9_]+'), re.compile(r'"([^"\\\n]{1,128})"'),
                          re.compile(r"'([^'\\\n]{1,128})'"))


def name_digests(value, found=None):
    """Every SHA-256 under an `input_digests` key (object keys or list items) at any depth of a JSON value."""
    found = set() if found is None else found
    if isinstance(value, dict):
        for key, item in value.items():
            if key == 'input_digests' and isinstance(item, (dict, list)):
                found.update(d.lower() for d in item if isinstance(d, str) and HEX_DIGEST.match(d.lower()))
            name_digests(item, found)
    elif isinstance(value, list):
        for item in value:
            name_digests(item, found)
    return found


def digest_name_findings(root, paths):
    """A shipped contract pins private names by SHA-256 only (`input_digests`). Fail every line, in any tracked
    file (exempt files included), carrying a word or quoted string whose SHA-256 is one of those digests: the
    plain name next to its digest defeats the hashing. The finding names the digest, never the matched text."""
    digests = {}
    for path in paths:
        if path.suffix != '.json':
            continue
        try:
            value = json.loads(path.read_text(encoding='utf-8'))
        except (UnicodeDecodeError, OSError, ValueError):
            continue
        for digest in name_digests(value):
            digests.setdefault(digest, path.relative_to(root).as_posix())
    if not digests:
        return
    for path in paths:
        try:
            text = path.read_text(encoding='utf-8')
        except (UnicodeDecodeError, OSError):
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            words = {m.group(m.lastindex or 0) for p in DIGEST_NAME_CANDIDATES for m in p.finditer(line)}
            for word in sorted(words):
                digest = hashlib.sha256(word.encode('utf-8', 'surrogatepass')).hexdigest()
                if digest in digests:
                    yield (path.relative_to(root).as_posix(), lineno,
                           f'plain-text name of a hashed input in {digests[digest]}', f'sha256 {digest[:12]}...')


def scan_exempt_file(path, extra):
    """Owner patterns only: an EXEMPT_FILES entry skips the built-in generic
    rules, never the owner's real values."""
    if not extra:
        return []
    try:
        text = path.read_text(encoding='utf-8')
    except (UnicodeDecodeError, OSError):
        return []
    return list(scan_lines(text.splitlines(), extra, generic=False))


def run(root):
    root = Path(root).resolve()
    extra = load_extra_patterns()
    patterns = dict(PATTERNS)
    patterns.update(extra)
    total = 0
    paths = [path for path in tracked_files(root) if path.is_file()]
    for path in paths:
        rel = path.relative_to(root).as_posix()
        found = scan_exempt_file(path, extra) if rel in EXEMPT_FILES else scan_file(path, patterns)
        for lineno, reason, snippet in found:
            print(f'{rel}:{lineno}: {reason}: {snippet!r}', file=sys.stderr)
            total += 1
        # The path string itself (not just its contents) must also clear the
        # built-in + owner patterns: a leak-shaped file/directory name (e.g.
        # a real home path or email address used as a path segment) is a
        # leak even in a file whose content is clean.
        for _, reason, snippet in scan_lines((rel,), patterns):
            print(f'{rel} (path): {reason}: {snippet!r}', file=sys.stderr)
            total += 1
    for rel, lineno, reason, snippet in digest_name_findings(root, paths):
        print(f'{rel}:{lineno}: {reason}: {snippet}', file=sys.stderr)
        total += 1
    return total


def bad_identities(root):
    """Every commit author/committer (all refs) and every annotated tag's
    tagger that is not exactly APPROVED_PUBLIC_IDENTITY."""
    bad = set()
    commits = subprocess.run(
        ['git', '-C', str(root), 'log', '--all', '--format=%an <%ae>%n%cn <%ce>'],
        capture_output=True, text=True, check=True)
    for line in commits.stdout.splitlines():
        line = line.strip()
        if line and line != APPROVED_PUBLIC_IDENTITY:
            bad.add(line)
    tags = subprocess.run(
        ['git', '-C', str(root), 'for-each-ref', 'refs/tags', '--format=%(taggername) %(taggeremail)'],
        capture_output=True, text=True, check=True)
    for line in tags.stdout.splitlines():
        line = line.strip()
        if not line or line == '<>':
            continue  # lightweight tag: no tagger identity at all
        normalized = re.sub(r'\s+', ' ', line).replace('< ', '<').replace(' >', '>')
        if normalized != APPROVED_PUBLIC_IDENTITY:
            bad.add(line)
    return sorted(bad)


# Raw git date: "<unix seconds> <offset>". Public history must be UTC.
RAW_DATE = re.compile(r'^(?P<seconds>-?\d+) (?P<offset>[+-]\d{4})$')
PUBLIC_OFFSET = '+0000'


def bad_offsets(root):
    """Every commit author/committer date (all refs) and every annotated
    tag's tagger date whose UTC offset is not exactly +0000. A local offset
    (e.g. +0200) is a timezone fingerprint of whoever made the commit."""
    bad = []
    commits = subprocess.run(
        ['git', '-C', str(root), 'log', '--all', '--format=%H %ad|%cd', '--date=raw'],
        capture_output=True, text=True, check=True)
    for line in commits.stdout.splitlines():
        if not line.strip():
            continue
        sha, _, dates = line.partition(' ')
        for role, raw in zip(('author', 'committer'), dates.split('|')):
            match = RAW_DATE.match(raw.strip())
            if not match or match.group('offset') != PUBLIC_OFFSET:
                bad.append(f'commit {sha[:12]} {role} date {raw.strip()!r}')
    tags = subprocess.run(
        ['git', '-C', str(root), 'for-each-ref', 'refs/tags',
         '--format=%(refname:short)|%(taggerdate:raw)'],
        capture_output=True, text=True, check=True)
    for line in tags.stdout.splitlines():
        name, _, raw = line.partition('|')
        raw = raw.strip()
        if not raw:
            continue  # lightweight tag: no tagger date at all
        match = RAW_DATE.match(raw)
        if not match or match.group('offset') != PUBLIC_OFFSET:
            bad.append(f'tag {name} tagger date {raw!r}')
    return bad


def tag_texts(root):
    """(where, text) for every tag name and every annotated tag's message
    (lightweight tags have no message of their own; their commit message is
    already part of `git log --all`)."""
    out = subprocess.run(
        ['git', '-C', str(root), 'for-each-ref', 'refs/tags',
         '--format=%(refname:short)%00%(objecttype)%00%(contents)%00%00'],
        capture_output=True, text=True, check=True)
    for record in out.stdout.split('\0\0'):
        record = record.lstrip('\n')
        if not record:
            continue
        name, _, rest = record.partition('\0')
        objecttype, _, message = rest.partition('\0')
        yield f'history/tag-name {name}', name
        if objecttype == 'tag':
            yield f'history/tag-message {name}', message


def history_paths(root):
    """Every file path that ever existed on any ref (adds, renames, deletes)."""
    out = subprocess.run(
        ['git', '-C', str(root), 'log', '--all', '--format=', '--name-only', '--no-renames', '-z'],
        capture_output=True, text=True, check=True)
    return sorted({p.strip('\n') for p in out.stdout.split('\0') if p.strip('\n')})


DIFF_NEW_FILE = re.compile(r'^\+\+\+ (?:b/(?P<path>.+)|/dev/null)\n?$')


def diff_lines_with_path(stream):
    """Yield (lineno, line, path) from `git log -p` output. path is the
    current file's repo-relative path while inside its hunk (from the most
    recent '+++ b/<path>' marker), or None between files/outside any diff
    (commit messages, 'diff --git' headers, old-side '--- a/<path>', ...)."""
    path = None
    for lineno, line in enumerate(stream, 1):
        marker = DIFF_NEW_FILE.match(line)
        if marker:
            path = marker.group('path')  # None for a deleted file (+++ /dev/null)
            continue
        if line.startswith('diff --git '):
            path = None
        yield lineno, line, path


def decoded_log_lines(stream):
    """Decode `git log -p --text` output line by line. Yields (line, ok):
    ok is False when the line's bytes are not valid UTF-8 (binary content
    forced through --text), with the line decoded leniently for display."""
    for raw in stream:
        try:
            yield raw.decode('utf-8'), True
        except UnicodeDecodeError:
            yield raw.decode('utf-8', errors='replace'), False


def run_history(root):
    """git log --all -p --text, scanned the same way as a tracked file
    (including the same EXEMPT_FILES, matched against each diff hunk's own
    path; exempt hunks still get the owner patterns), plus every
    author/committer/tagger identity and date offset, every tag name and
    annotated tag message, and every historical file path (owner patterns).
    Slow; public-repo use only.

    Same binary rule as the tree scan: a file's diff that does not decode as
    UTF-8 is skipped (its bytes, forced through --text, otherwise match
    short private patterns at random). Each file's diff is buffered until
    the next file starts, so that decision covers the whole diff. Pine is
    checked here too (a .pine path, or Pine source in a file's diff), so a
    Pine file added and later removed still fails the history gate."""
    root = Path(root).resolve()
    total = 0
    for identity in bad_identities(root):
        print(f'history: unapproved commit/tag identity: {identity!r}', file=sys.stderr)
        total += 1
    for where in bad_offsets(root):
        print(f'history: non-UTC commit/tag date (want {PUBLIC_OFFSET}): {where}', file=sys.stderr)
        total += 1
    extra = load_extra_patterns()
    patterns = dict(PATTERNS)
    patterns.update(extra)

    def report(where, lineno, reason, snippet):
        nonlocal total
        print(f'{where}:{lineno}: {reason}: {snippet!r}', file=sys.stderr)
        total += 1

    for where, text in tag_texts(root):
        for lineno, reason, snippet in scan_lines(text.splitlines(), patterns):
            report(where, lineno, reason, snippet)
    for path in history_paths(root):
        # Historical/deleted paths get the same built-in + owner patterns as
        # a live path in run() above, not just the owner's extra patterns.
        for _, reason, snippet in scan_lines((path,), patterns):
            report('history/path', 1, reason, snippet)

    def flush(path, hunk, binary):
        if path is None or not hunk:
            return
        where = f'history/{path}'
        if path in EXEMPT_FILES:
            if not binary:
                for lineno, line in hunk:
                    for _, reason, snippet in scan_lines((line,), extra, generic=False):
                        report(where, lineno, reason, snippet)
            return
        if path.endswith('.pine'):
            report(where, hunk[0][0], 'Pine source file (.pine extension)', Path(path).name)
            return
        if binary:
            return
        for lineno, line in hunk:
            for _, reason, snippet in scan_lines((line,), patterns):
                report(where, lineno, reason, snippet)
        # Pine detection on the diff body with the one-char +/-/space marker
        # stripped, so a '+//@version=5' line is seen as '//@version=5'.
        body = '\n'.join(line[1:] if line[:1] in '+- ' else line for _, line in hunk)
        first = hunk[0][0]
        for offset, reason, snippet in pine_findings(Path(path), body):
            report(where, first + offset - 1, reason, snippet)

    process = subprocess.Popen(
        ['git', '-C', str(root), 'log', '--all', '-p', '--text'],
        stdout=subprocess.PIPE)
    current, hunk, binary = None, [], False
    try:
        lines = decoded_log_lines(process.stdout)
        flags = {}

        def text_only():
            for text, ok in lines:
                flags['ok'] = ok
                yield text

        for lineno, line, path in diff_lines_with_path(text_only()):
            if path != current:
                flush(current, hunk, binary)
                current, hunk, binary = path, [], False
            if path is None:
                for _, reason, snippet in scan_lines((line,), patterns):
                    report('history', lineno, reason, snippet)
                continue
            hunk.append((lineno, line))
            if not flags.get('ok', True):
                binary = True
        flush(current, hunk, binary)
    finally:
        process.stdout.close()
        process.wait()
    return total


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', default='.', help='repo root to scan (default: cwd)')
    parser.add_argument('--history', action='store_true',
                         help='also scan full git history and check author/committer/tagger identities '
                              'and +0000 date offsets '
                              '(public repo only -- see module docstring)')
    args = parser.parse_args()
    total = run(args.root)
    if args.history:
        total += run_history(args.root)
    if total:
        print(f'privacy_check: {total} finding(s) -- fix before export', file=sys.stderr)
        return 1
    print('privacy_check: clean', file=sys.stderr)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
