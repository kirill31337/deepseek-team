import math
import unittest

from codex_deepseek_team import routing_models as m


class RoutingModelsTests(unittest.TestCase):
    def test_feature_card_is_closed_and_unknown_by_default(self):
        self.assertEqual(m.validate_features({})['risk'], 'unknown')
        with self.assertRaises(m.RoutingError):
            m.validate_features({'prompt': 'private code'})

    def test_invalid_numbers_and_boolean_numbers_rejected(self):
        for value in (math.nan, math.inf, -1, True, 10 ** 1000):
            with self.subTest(value=value), self.assertRaises(m.RoutingError):
                m.validate_config({'external_weight_cap': value})

    def test_json_byte_limit_counts_utf8_bytes(self):
        with self.assertRaises(m.RoutingError):
            m.read_json('"' + '\u20ac' * (m.MAX_JSON_BYTES // 2) + '"')

    def test_configuration_is_merged_and_validated(self):
        config = m.validate_config({'mode': 'auto'})
        self.assertEqual(config['mode'], 'auto')
        self.assertTrue(config['require_cost_evidence'])
        with self.assertRaises(m.RoutingError):
            m.validate_config({'mode': 'enabled'})

    def test_observation_has_no_free_form_text_and_no_future_outcome(self):
        row = dict(id='one', case_id='case', origin='local', features={},
                   action='worker', outcome='accepted', observed_at=100)
        self.assertIsNone(m.validate_observation(row, now=101)['cost_usd'])
        for extra in ({'notes': 'secret'}, {'observed_at': 1000}, {'cost_usd': -1}):
            with self.subTest(extra=extra), self.assertRaises(m.RoutingError):
                m.validate_observation(dict(row, **extra), now=101)

    def test_external_evidence_requires_provenance(self):
        with self.assertRaises(m.RoutingError):
            m.validate_observation(dict(id='one', case_id='case', origin='external',
                                       features={}, action='worker', outcome='accepted', observed_at=100), now=101)

    def test_limits_and_unknown_settings_fail(self):
        for values in ({'confidence': 1}, {'min_success_probability': 0},
                       {'arbitrary': 1}, {'require_cost_evidence': 'false'}):
            with self.subTest(values=values), self.assertRaises(m.RoutingError):
                m.validate_config(values)
