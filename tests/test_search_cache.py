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

    def test_saturated_science_can_still_complete_a_new_request(self):
        p=Planner(fixture())
        p.factor=[1.0]*len(p.ids)
        p.cur=[1.2]*len(p.ids)
        p.on_requests([request(['near'])])
        action=p.plan(START,END,0,0)
        self.assertIsNotNone(action)
        self.assertIn('near',action['assignments'].values())

    def test_dynamic_grid_and_fast_density_preserve_physical_assignments(self):
        from skymath import local_sidereal_deg,radec_to_altaz,tangent_offsets
        for side in (3,4,5,10):
            for level in (0,1,2,3):
                with self.subTest(side=side,level=level),patch('planner.DYNAMIC_GEOMETRY',1),patch('planner.FAST_DENSE',4):
                    data=fixture();data['instrument'].update(grid_side=side,n_fibers=side*side,
                        pitch_deg=2.4/side,glass_side_deg=2.4/side,fov_side_deg=2.4)
                    p=Planner(data);p.fast_level=level
                    action=p.plan(START,END,0,0)
                    self.assertIsNotNone(action)
                    for fiber,target in action['assignments'].items():
                        i=p.index_of[target]
                        alt,az=radec_to_altaz(p.ra[i],p.dec[i],local_sidereal_deg(START,p.lon),p.lat)
                        actual=p.grid.classify(*tangent_offsets(alt,az,action['pointing']['alt_deg'],action['pointing']['az_deg']))[0]
                        self.assertEqual(actual,int(fiber))

    def test_joint_program_matches_best_forced_search(self):
        rng=random.Random(17)
        data=fixture();ra=data['targets']['rows'][0][1]
        data['targets']['rows']=[[str(i),ra+rng.uniform(-3,3),rng.uniform(-3,3),rng.uniform(.2,2),rng.uniform(.5,2),False] for i in range(80)]
        scores={}
        with patch('planner.JOINT_PROGRAM',1),patch('planner.REFINE_STEPS',()):
            for program in (None,'DARK','BRIGHT','BACKUP'):
                p=Planner(copy.deepcopy(data));p.force_program=program
                p.band_obs.clear();p.scale=.5;p.prior_scale=.5
                action=p.plan(START,END,0,0)
                if program:self.assertEqual(action['program'],program)
                scores[program]=p.plan_metrics['net_gain']
        self.assertAlmostEqual(scores[None],max(scores[x] for x in ('DARK','BRIGHT','BACKUP')))

    def test_completion_duration_reaches_threshold_in_gain_model(self):
        p=Planner(fixture());i=p.index_of['near']
        for m0,m1 in ((.6,.8),(.9,.8),(.5,.5)):
            t=p.completion_duration(i,m0,m1,.8,1,3600)
            self.assertIsNotNone(t)
            reach=p.flux[i]*t*(m0+(m1-m0)*t/3600)*.8/p.f0t0
            self.assertGreaterEqual(reach,1)
            self.assertIsNone(p.completion_duration(i,m0,m1,.8,1,t-1))
        self.assertIsNone(p.completion_duration(i,1,0,1,100,3600))

    def test_shared_projection_matches_original_including_zenith(self):
        from skymath import tangent_offsets,project_vector,unit_vector,tangent_frame
        rng=random.Random(123)
        for _ in range(1000):
            alt,az=rng.uniform(-90,90),rng.uniform(-360,720)
            ca,cz=rng.choice([90,0,rng.uniform(-90,90)]),rng.uniform(-360,720)
            self.assertEqual(tangent_offsets(alt,az,ca,cz),project_vector(unit_vector(alt,az),tangent_frame(ca,cz)))

    def test_shared_projection_preserves_actual_planning_action(self):
        actions=[]
        for cache in (0,1):
            with patch('planner.PROJECT_CACHE',cache):
                p=Planner(fixture())
                actions.append(p.plan(START,END,0,0))
        self.assertEqual(*actions)


if __name__=='__main__':
    unittest.main()
