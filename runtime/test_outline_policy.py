import json
import unittest
from pathlib import Path

from router_policy import PolicyError, decide, build_packet
from main_switch import recommend
from test_router_policy import task, CATALOG, whole_task


def outlined(**kwargs):
    return task(mode='outline', operation='business', resources=[], read_paths=[], snapshot_files={}, artifacts=[], **kwargs)


class OutlinePolicyTests(unittest.TestCase):
    def test_explicit_null_pins_rejected(self):
        with self.assertRaises(PolicyError): recommend(task(), CATALOG, None, True)

    def test_controller_stays_astra_across_business_stages(self):
        for features in ({'deterministic': True}, {'stage': 'planning'},
                         {'stage': 'review'}, {'stage': 'research', 'multi_source': True}):
            t = outlined(features=features)
            self.assertEqual(recommend(t, CATALOG, {}, True)['target'], t['current'])
        ordinary = task(features={'deterministic': True})
        self.assertEqual(recommend(ordinary, CATALOG, {}, True)['target']['model'], 'gpt-6.2-luna')

    def test_non_astra_requires_switch_or_explicit_user_exception(self):
        t = outlined(current={'model': 'gpt-6.2-sol'})
        self.assertEqual(decide(t, CATALOG)['action'], 'blocked')
        self.assertEqual(recommend(t, CATALOG, {}, False)['target']['model'], 'gpt-6-astra')
        for pins in ({'model': 'gpt-6.2-sol', 'source': 'user'},
                     {'no_switch': True, 'source': 'user'}):
            t['controller_pins'] = pins
            self.assertEqual(decide(t, CATALOG)['model'], 'gpt-6.2-luna')
            self.assertEqual(recommend(t, CATALOG, {}, True)['target']['model'], 'gpt-6.2-sol')
        t['controller_pins'] = {'effort': 'high', 'source': 'user'}
        self.assertEqual(decide(t, CATALOG)['action'], 'blocked')

    def test_pins_conflicts_and_unmet_fields_block(self):
        t = outlined(controller_pins={'effort': 'max', 'source': 'user'})
        self.assertEqual(decide(t, CATALOG)['action'], 'blocked')
        t['controller_pins'] = {'model': 'gpt-6-astra', 'source': 'user'}
        self.assertEqual(recommend(t, CATALOG, {'model': 'gpt-6.2-sol', 'source': 'user2'}, True)['action'], 'blocked')
        t['override'] = whole_task('gpt-6.2-sol')
        self.assertEqual(decide(t, CATALOG)['action'], 'blocked')

    def test_outline_small_business_delegates_normal_small_task_direct(self):
        f = {'risk': 'low', 'complexity': 'simple', 'deterministic': True}
        self.assertEqual(decide(outlined(features=f), CATALOG)['role'], 'luna_worker')
        self.assertEqual(decide(task(features=f), CATALOG)['action'], 'direct')
        t = outlined(features={'stage': 'strategy'}); t['operation'] = 'controller'
        self.assertEqual(decide(t, {})['role'], 'astra_controller')

    def test_unresolved_and_high_risk_business_requires_controller(self):
        for f in ({'risk': 'high'}, {'stage': 'strategy'}, {'ambiguity': 'conflict'}):
            self.assertEqual(decide(outlined(features=f), CATALOG)['action'], 'blocked')
            self.assertEqual(decide(task(features=f), CATALOG)['action'], 'direct')

    def test_prohibition_preserved_but_missing_authority_never_silent_direct(self):
        self.assertEqual(decide(outlined(delegation={'authorized': False}), CATALOG)['action'], 'blocked')
        d = decide(outlined(delegation={'authorized': False, 'prohibited': True}), CATALOG)
        self.assertEqual(d['action'], 'direct'); self.assertTrue(d['limitations'])
        t = outlined(current={'model': 'gpt-6.2-sol'}, delegation={'prohibited': True})
        self.assertEqual(decide(t, CATALOG)['action'], 'direct')
        self.assertEqual(recommend(t, CATALOG, {}, False)['target'], t['current'])
        t['controller_pins'] = {'effort': 'max', 'source': 'user'}
        self.assertEqual(decide(t, CATALOG)['action'], 'blocked')

    def test_controller_label_cannot_erase_independent_review(self):
        for stage in ('review', 'planning', 'execution', 'research'):
            t = outlined(features={'stage': stage}); t['operation'] = 'controller'
            with self.assertRaises(PolicyError): decide(t, CATALOG)

    def test_whole_task_override_preserves_role_effort(self):
        t = outlined(current={'model': 'gpt-6.2-sol'}, override=whole_task('gpt-6.2-sol'))
        self.assertEqual((decide(t, CATALOG)['model'], decide(t, CATALOG)['effort']), ('gpt-6.2-sol', 'medium'))
        t['override']['effort'] = 'high'
        self.assertEqual(decide(t, CATALOG)['effort'], 'high')

    def test_no_fallback_when_required_coordinator_unavailable(self):
        t = outlined(current={'model': 'gpt-6.2-sol'})
        self.assertEqual(recommend(t, {'source': 'runtime-tool', 'models': []}, {}, True)['action'], 'blocked')

    def test_schema_and_packet_preserve_mode(self):
        for patch in ({'mode': 'outlien'}, {'operation': None},
                      {'controller_pins': {'model': 'gpt-6-astra'}},
                      {'controller_pins': {'no_switch': 1, 'source': 'user'}}):
            t = outlined(); t.update(patch)
            with self.assertRaises(PolicyError): decide(t, CATALOG)
        t = outlined(controller_pins={'effort': 'high', 'source': 'user'})
        p = build_packet(t, decide(t, CATALOG), Path(__file__).parent/'agents', 'outline-test')
        body = json.loads(p['spawn_args']['message'].split('\n', 1)[1])
        self.assertEqual(body['mode'], 'outline'); self.assertEqual(body['operation'], 'business')
        self.assertEqual(body['controller_pins'], t['controller_pins'])


if __name__ == '__main__': unittest.main()
