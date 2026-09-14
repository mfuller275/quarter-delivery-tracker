import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from backend import squads


class SquadLeadTests(unittest.TestCase):
    def test_team_objects_keep_named_leads(self):
        payload = {
            "portfolios": [
                {
                    "name": "Core",
                    "productSquads": [
                        {
                            "name": "Core",
                            "scrumTeams": [
                                {"name": "Nucleons", "lead": "Alyssa"},
                                {"name": "Vanquishers", "lead": "Marcus"},
                            ],
                        }
                    ],
                }
            ]
        }

        with patch("backend.squads.get_settings", return_value=SimpleNamespace(squads_file="squads.json")):
            with patch.object(squads.Path, "read_text", return_value=json.dumps(payload)):
                idx = squads.build_index()

        self.assertEqual(idx["teamIndex"]["nucleons"]["teamLead"], "Alyssa")
        self.assertEqual(idx["teamIndex"]["vanquishers"]["teamLead"], "Marcus")

    def test_string_team_names_still_work(self):
        payload = {
            "portfolios": [
                {
                    "name": "Core",
                    "productSquads": [
                        {
                            "name": "Core",
                            "scrumTeams": ["Nucleons", "Vanquishers"],
                        }
                    ],
                }
            ]
        }

        with patch("backend.squads.get_settings", return_value=SimpleNamespace(squads_file="squads.json")):
            with patch.object(squads.Path, "read_text", return_value=json.dumps(payload)):
                idx = squads.build_index()

        self.assertEqual(idx["teamIndex"]["nucleons"]["teamLead"], None)
        self.assertEqual(idx["teamIndex"]["vanquishers"]["teamLead"], None)


if __name__ == "__main__":
    unittest.main()
