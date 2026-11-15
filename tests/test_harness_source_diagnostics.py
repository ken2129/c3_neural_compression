import unittest
from urllib.error import HTTPError,URLError
from research_harness.service import source_failure_report,source_retry_delay
from research_harness.schema import ContractError

class SourceDiagnosticsTests(unittest.TestCase):
    def test_rate_status_only(self):
        e=HTTPError('https://secret.invalid/token',403,'SECRET',{'X-RateLimit-Remaining':'0','X-RateLimit-Reset':'1792230300','Authorization':'SECRET'},None)
        self.assertEqual(source_failure_report(e),dict(phase='SOURCE_UNAVAILABLE',dispatched=False,error_category='HTTP_ERROR',http_status=403,rate_remaining=0,rate_reset_unix=1792230300))
    def test_untrusted_headers_and_errors_are_not_output(self):
        for e in (HTTPError('SECRET',403,'SECRET',{'X-RateLimit-Remaining':'SECRET','X-RateLimit-Reset':'9'*100},None),ValueError('SECRET'),URLError('SECRET'),ContractError('SECRET'),TimeoutError('SECRET')):
            self.assertNotIn('SECRET',str(source_failure_report(e)))
    def test_fixed_categories(self):
        for e,c in [(URLError('x'),'TRANSPORT_ERROR'),(TimeoutError('x'),'TIMEOUT'),(ContractError('x'),'SOURCE_CONTRACT_ERROR'),(ValueError('x'),'OTHER')]:
            self.assertEqual(source_failure_report(e)['error_category'],c)

    def test_waits_for_rate_reset(self):
        reports=[dict(phase='SOURCE_UNAVAILABLE',http_status=403,rate_reset_unix=2000)]
        self.assertEqual(source_retry_delay(reports,now=1000),1001)
        self.assertEqual(source_retry_delay([],now=1000),60)
        reports[0]['rate_reset_unix']='SECRET'
        self.assertEqual(source_retry_delay(reports,now=1000),300)
