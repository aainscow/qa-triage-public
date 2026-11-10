"""Normalise log text so that the same failure looks the same in every job.

Hosts, numbers, hashes, addresses and timestamps are masked, in the spirit of
template miners such as Drain: two lines with the same template differ only in
such variables.
"""

import re

_MASKS = [
    (re.compile(r'\d{4}-\d\d-\d\dT[\d:.]+(\+\d{4}|Z)?'), '<time>'),
    (re.compile(r'\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b'),
     '<uuid>'),
    (re.compile(r'\b(?:\d{1,3}\.){3}\d{1,3}(?::\d+)?(?:/\d+)?\b'), '<ip>'),
    (re.compile(r'\b(?:trial|smithi|gibba|mira|o)\d+(?:\.front\.sepia\.ceph\.com)?\b'),
     '<host>'),
    # A client named after its host, such as a mgr's libcephfs session, trial174:y.
    (re.compile(r'(?<=<host>):[A-Za-z][\w-]*'), ':<name>'),
    (re.compile(r'\b\d+\.[0-9a-f]+(?:s\d+)?\b'), '<pg>'),    # 3.bs0: a PG, with its shard
    (re.compile(r'\b0x[0-9a-fA-F]+\b'), '<hex>'),
    (re.compile(r'\b(?=[0-9]*[a-f])[0-9a-f]{7,40}\b'), '<sha>'),    # not a long number
    (re.compile(r'\d+'), '#'),
]


def template(text):
    for regex, mask in _MASKS:
        text = regex.sub(mask, text)
    return text


def fingerprint(job):
    """The failure reason with its variables masked; jobs that share it most
    likely failed the same way."""
    return template(job.reason.strip()) or f'<{job.status} with no reason>'


def compact(numbered_lines):
    """Keep the first line of each template, noting how many more there were.

    numbered_lines is [(line number, text)]; returns ["number: text (+N similar)"].
    """
    first, counts = {}, {}
    for n, text in numbered_lines:
        key = template(text.split(' ', 2)[-1])  # ignore teuthology's timestamp
        if key not in first:
            first[key] = (n, text)
            counts[key] = 0
        else:
            counts[key] += 1
    return [f'{n}: {text}' + (f'  (+{counts[k]} similar)' if counts[k] else '')
            for k, (n, text) in first.items()]


_MASK_PATTERNS = {'<time>': r'\S+', '<uuid>': r'\S+', '<ip>': r'\S+', '<host>': r'\S+',
                  '<name>': r'[A-Za-z][\w-]*', '<pg>': r'\S+',
                  '<hex>': r'\S+', '<sha>': r'\S+', '#': r'\d+'}


def to_regex(text):
    """A regex that matches text and anything with the same template."""
    out = re.escape(template(text))
    for mask, pattern in _MASK_PATTERNS.items():
        out = out.replace(re.escape(mask), pattern)
    # re.escape() is more thorough than a reader needs.
    return out.replace('\\ ', ' ').replace('\\-', '-')
