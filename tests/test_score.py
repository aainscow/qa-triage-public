import json
import os
import tempfile
import unittest

import yaml

from fakes import RUN, problems, record

from qa_triage import score


class TestScore(unittest.TestCase):
    def test_score(self):
        top = tempfile.mkdtemp()
        answers, runs = os.path.join(top, 'answers'), os.path.join(top, 'runs')
        os.makedirs(answers)
        os.makedirs(os.path.join(runs, RUN))
        with open(os.path.join(answers, 'index.yaml'), 'w') as f:
            yaml.safe_dump({RUN: 'a.yaml', 'not-triaged': 'b.yaml'}, f)
        # People's answers: 2 is 100's backport's original, 4 the problem 200 has
        # in also.
        with open(os.path.join(answers, 'a.yaml'), 'w') as f:
            yaml.safe_dump({1: 100, 2: 101, 3: 300, 4: 201, 5: 500, 6: 300}, f)
        results = [{'job': '1', 'problem': '100', 'sure': True},
                   {'job': '2', 'problem': '100', 'sure': True},
                   {'job': '3', 'problem': '100', 'sure': True},
                   {'job': '4', 'problem': '200', 'sure': True},
                   {'job': '5', 'problem': None, 'sure': True},
                   {'job': '6', 'problem': None, 'sure': False},
                   {'job': '7', 'problem': '100', 'sure': True}]
        with open(os.path.join(runs, RUN, 'results.json'), 'w') as f:
            json.dump(results, f)
        triaged = score.runs(answers, runs)
        self.assertEqual([run for run, _, _ in triaged], [RUN])
        found = score.score(triaged, problems(record(), record(id='200', also=['201'])),
                            lambda n: {'101': '100'}.get(n, n))
        self.assertEqual(dict(found['total']), {'jobs': 6, 'right': 3, 'also': 1,
                                                'sure, wrong': 2, 'unsure': 1})
        self.assertFalse(found['recordless'])
        self.assertEqual(dict(found['records']['100']), {'placed': 3, 'right': 2})
        self.assertEqual(dict(found['pairs']), {('100', '300'): 1})
        self.assertEqual(dict(found['unknown']), {'500': 1})
        text = '\n'.join(score.lines(found))
        self.assertIn('3 of 6 jobs people placed were placed the same (50%), 1 of them in a '
                      'record whose also names the people\'s issue;', text)
        self.assertIn('  100: 2 of 3 right', text)
        self.assertIn('  1 jobs: the tool said 100, people 300', text)
        self.assertIn('  1 jobs: 500', text)
        self.assertEqual([(said, answer, [r['job'] for _, r in rs])
                          for said, answer, rs in score.curriculum(found)],
                         [('100', '300', ['3']), (None, '500', ['5']), (None, '300', ['6'])])
        # What a person settled is the answer: 3 is 100, 6 is none, and 7 is 100.
        settled = score.runs(answers, runs, {f'{RUN}/3': '100', f'{RUN}/6': None,
                                             f'{RUN}/7': '100', f'other/1': '100'})
        self.assertEqual(settled[0][1], {'1': '100', '2': '101', '3': '100', '4': '201',
                                         '5': '500', '7': '100'})
        # No record names 300 or 500: each is one lesson, all its jobs at once.
        self.assertEqual([(said, answer, [r['job'] for _, r in rs])
                          for said, answer, rs in score.curriculum(found, {'100', '200'})],
                         [('100', '300', ['3', '6']), (None, '500', ['5'])])

    def test_the_memory_alone(self):
        top = tempfile.mkdtemp()
        answers, runs = os.path.join(top, 'answers'), os.path.join(top, 'runs')
        os.makedirs(answers)
        os.makedirs(os.path.join(runs, RUN))
        with open(os.path.join(answers, 'index.yaml'), 'w') as f:
            yaml.safe_dump({RUN: 'a.yaml'}, f)
        with open(os.path.join(answers, 'a.yaml'), 'w') as f:
            yaml.safe_dump({1: 300, 2: 300, 3: 300, 4: 500, 5: 600, 6: 600}, f)
        # The memory placed 1, 2, 4 and 5 in issues no record names; 3 and 6 the tool
        # got wrong.
        results = [{'job': '1', 'problem': '300', 'sure': True},
                   {'job': '2', 'problem': '300', 'sure': True},
                   {'job': '3', 'problem': None, 'sure': True},
                   {'job': '4', 'problem': '500', 'sure': True},
                   {'job': '5', 'problem': '600', 'sure': True},
                   {'job': '6', 'problem': '100', 'sure': True}]
        with open(os.path.join(runs, RUN, 'results.json'), 'w') as f:
            json.dump(results, f)
        found = score.score(score.runs(answers, runs), problems(record()))
        self.assertEqual(dict(found['total']), {'jobs': 6, 'right': 4, 'memory': 4, 'also': 0,
                                                'sure, wrong': 2})
        text = '\n'.join(score.lines(found))
        self.assertIn('4 of 6 jobs people placed were placed the same (66%), 4 of them by '
                      'the memory alone', text)
        self.assertIn('  2 jobs: 300', text)
        # Each still needs a record: the jobs the tool got wrong first, and the
        # memory's after them.
        self.assertEqual([(said, answer, [r['job'] for _, r in rs])
                          for said, answer, rs in score.curriculum(found, {'100'})],
                         [(None, '300', ['3', '1', '2']), ('100', '600', ['6', '5']),
                          (None, '500', ['4'])])


if __name__ == '__main__':
    unittest.main()
