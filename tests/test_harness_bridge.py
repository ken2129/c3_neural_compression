import unittest
from research_harness.bridge import FakeChatBridge, Envelope, UnsupportedLiveBridge
from research_harness.schema import ContractError


class BridgeTest(unittest.TestCase):
    def test_duplicate_readback_and_out_of_order(self):
        bridge = FakeChatBridge()
        revoke = bridge.send('r', 'request', 'revocation', {'cancel': True})
        approval = bridge.send('a', 'request', 'approval', {'fixture': True})
        self.assertEqual(bridge.send('a', 'request', 'approval', {'fixture': True}), approval)
        self.assertEqual(bridge.poll(), (revoke, approval))
        self.assertEqual(bridge.poll(0), (approval,))
        self.assertEqual(bridge.readback('a')[1], {'fixture': True})
        with self.assertRaises(ContractError): bridge.send('a', 'request', 'approval', {'fixture': False})
        with self.assertRaises(ContractError): bridge.readback('missing')

    def test_unknown_field_and_live_refused(self):
        with self.assertRaises(ContractError): Envelope.parse({'command': 'rm'})
        with self.assertRaises(ContractError): UnsupportedLiveBridge()
