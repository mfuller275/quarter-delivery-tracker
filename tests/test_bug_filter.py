import unittest
from datetime import date

from backend.config import Settings
from backend.metrics import compute_summary


class BugFilterTests(unittest.TestCase):
    def test_bug_items_are_excluded_from_summary_totals(self):
        settings = Settings()
        raw_items = [
            {
                "id": 1,
                "fields": {
                    "System.WorkItemType": "Bug",
                    "System.State": "Active",
                    "System.Title": "Bad bug",
                    "System.AreaPath": "PPM\\Team A",
                    "System.AssignedTo": {"displayName": "Alice"},
                    "System.IterationPath": "PPM\\Team A",
                    "System.Tags": "",
                    "Microsoft.VSTS.CMMI.Blocked": "No",
                    "Microsoft.VSTS.Scheduling.StoryPoints": 3,
                    "Microsoft.VSTS.Scheduling.TargetDate": date.today().isoformat(),
                },
            },
            {
                "id": 2,
                "fields": {
                    "System.WorkItemType": "User Story",
                    "System.State": "Done",
                    "System.Title": "Good story",
                    "System.AreaPath": "PPM\\Team A",
                    "System.AssignedTo": {"displayName": "Bob"},
                    "System.IterationPath": "PPM\\Team A",
                    "System.Tags": "",
                    "Microsoft.VSTS.CMMI.Blocked": "No",
                    "Microsoft.VSTS.Scheduling.StoryPoints": 5,
                    "Microsoft.VSTS.Scheduling.TargetDate": date.today().isoformat(),
                },
            },
        ]

        result = compute_summary(raw_items, settings)

        self.assertEqual(result["totals"]["totalItems"], 1)
        self.assertEqual(result["teams"][0]["totalItems"], 1)
        self.assertEqual(result["teams"][0]["team"], "PPM\\Team A")


if __name__ == "__main__":
    unittest.main()
