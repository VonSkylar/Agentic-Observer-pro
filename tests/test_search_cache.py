import copy
import random
import unittest
from datetime import timedelta
from unittest.mock import patch

from planner import Planner
from test_planning_constraints import fixture, START, END, request


class CellCacheTests(unittest.TestCase):
    def test_live_actions_equal_with_feedback_requests_and_environment(self):
        data = fixture()
        rng = random.Random(2607)
        ra = data['targets']['rows'][0][1]
        data['targets']['rows'] = [[f'T{i}',ra+rng.uniform(-6,6),rng.uniform(-5,5),
                                   rng.uniform(.1,2),rng.uniform(.3,2),i%13==0] for i in range(150)]
        for level in (0,1,2,3):
            with self.subTest(level=level):
                a,b = Planner(copy.deepcopy(data)),Planner(copy.deepcopy(data))
                a.fast_level=b.fast_level=level
                for turn in range(3):
                    now=START+timedelta(minutes=20*turn)
                    for planner in (a,b):
                        planner.on_requests([request(['T0','T1','T2'],count=1)])
                        planner.notices={('haze','S')} if turn==1 else set()
                    with patch('planner.CELL_CACHE',0):
                        x=a.plan(now,END,0,turn/3)
                    with patch('planner.CELL_CACHE',1):
                        y=b.plan(now,END,0,turn/3)
                    self.assertEqual(x,y)
                    self.assertEqual(a.rate_ema,b.rate_ema)
                    self.assertIsNotNone(x)
                    if level==0:
                        self.assertGreater(b.cell_cache_info['hits'],0)
                    result={'action':'observe','hits':[{'target_id':t,'score':.1} for t in x['assignments'].values()]}
                    for planner in (a,b):
                        planner.on_result(result,now+timedelta(seconds=x['duration_seconds']),turn/3)


if __name__=='__main__':
    unittest.main()
