import unittest
from unittest.mock import patch, MagicMock
import io
import json

from litreview.fetcher import clean_doi, fetch_crossref


class TestFetcher(unittest.TestCase):
    def test_clean_doi(self):
        self.assertEqual(clean_doi("https://doi.org/10.1016/j.fuel.2023.128741"), "10.1016/j.fuel.2023.128741")
        self.assertEqual(clean_doi("doi: 10.1000/182."), "10.1000/182")
        self.assertEqual(clean_doi("10.1000/182;"), "10.1000/182")
        self.assertEqual(clean_doi("not a doi"), "")

    @patch("urllib.request.urlopen")
    def test_fetch_crossref_success(self, mock_urlopen):
        mock_response = MagicMock()
        mock_response.status = 200
        data = {
            "message": {
                "title": ["Sample Academic Paper Title"],
                "author": [
                    {"given": "John", "family": "Doe"},
                    {"given": "Jane", "family": "Smith"}
                ],
                "container-title": ["Journal of Testing"],
                "published-print": {"date-parts": [[2024, 5, 1]]},
                "publisher": "Elsevier"
            }
        }
        mock_response.read.return_value = json.dumps(data).encode("utf-8")
        mock_urlopen.return_value.__enter__.return_value = mock_response

        res = fetch_crossref("10.1016/sample.2024")
        self.assertIsNotNone(res)
        self.assertEqual(res["title"], "Sample Academic Paper Title")
        self.assertEqual(res["authors"], "John Doe, Jane Smith")
        self.assertEqual(res["journal"], "Journal of Testing")
        self.assertEqual(res["year"], "2024")
        self.assertEqual(res["source"], "crossref")

    @patch("urllib.request.urlopen")
    def test_fetch_crossref_error_fallback(self, mock_urlopen):
        mock_urlopen.side_effect = Exception("Network error")
        res = fetch_crossref("10.1016/fail")
        self.assertIsNone(res)


if __name__ == "__main__":
    unittest.main()
