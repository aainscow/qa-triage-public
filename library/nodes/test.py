import re

from qa_triage.tree.common import before, failure, short
from qa_triage.tree.core import done, judge, node

# Jobs it was written or checked for, with the path each takes.
EXAMPLES = {
    'lflores-2026-07-17_23:28:16-rados-wip-lflores-testing-3-2026-07-17-1417-distro-default-trial/374651': 'test/unittest',
    'skanta-2026-03-25_22:46:45-rados-wip-bharath8-testing-2026-03-25-1848-distro-default-trial/122160': 'test/unittest',
    'skanta-2026-06-04_09:53:35-rados-wip-bharath5-testing-2026-06-02-1032-distro-default-trial/263736': 'test/unexplained',
}


@node('test', 'Which test failed, and on what?')
def test(ctx):
    fail = failure(ctx)
    failed = ctx.hits('unittest')
    if failed:
        first = failed[0]
        name, where = ctx.match('unittest', first).groups()[1:]
        # Python 3.11 and later give Class.method in the parentheses.
        test = where if where.endswith('.' + name) else f'{where}.{name}'
        error = [h for h in ctx.between(first.line, first.line + 200)
                 if re.search(r'(^|:)\s*(\w+\.)*\w*(Error|Exception): ', h.text)]
        said = re.sub(r'^.*?:\s*(?=(\w+\.)*\w*(Error|Exception): )', '',
                      error[0].text) if error else ''
        return done('unittest', f'{test} failed' + (f': {short(said)}' if said else ''),
                    first, *(error[:1]), key=f'{test}: {said}', test=test, error=short(said))
    gtests = before(ctx.hits('gtest'), fail.line) if fail else ctx.hits('gtest')
    if gtests:
        name = ctx.match('gtest', gtests[0]).group(1)
        return done('gtest', f'the gtest {name} failed', gtests[0], key=name, test=name)
    return judge('Which test failed?', 'the log does not show which test failed', fail,
                 branch='unexplained', key=ctx.job.reason)
