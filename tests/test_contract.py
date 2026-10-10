import tempfile
import unittest
from pathlib import Path

from arc_pi.contract import contract_notes, seed_values, seeds_only_in_data


def node(description, *givens):
    return {"id": "REQ-1", "description": description,
            "scenarios": [{"name": "s", "steps": [{"keyword": "GIVEN", "content": text}
                                                   for text in givens]}]}


def app(root: Path, frontend: str, backend: str, data: str | None = None) -> Path:
    (root / "frontend" / "src").mkdir(parents=True)
    (root / "backend" / "src").mkdir(parents=True)
    (root / "frontend" / "src" / "App.jsx").write_text(frontend, encoding="utf-8")
    (root / "backend" / "src" / "seed.js").write_text(backend, encoding="utf-8")
    if data is not None:
        (root / "backend" / "data").mkdir()
        (root / "backend" / "data" / "db.json").write_text(data, encoding="utf-8")
    return root


class ContractTests(unittest.TestCase):
    def test_values_a_scenario_creates_are_never_seed_records(self):
        seeds = seed_values([node(
            "",
            "The seeded data is board `Demo Group`, list `demo-docs`. The candidate "
            "board identifier is `new-group` and does not exist in the system.",
            "The target new file path `docs/guide.md` does not yet exist; the attempted "
            "descendant is `child-list`.",
            "The evaluation seed contains rows `Alpha/300` and expression `=X1+Y1`.")])
        self.assertEqual(seeds, ["Demo Group", "demo-docs"])

    def test_missing_names_and_seeds_are_reported_but_example_inputs_are_not(self):
        nodes = [node('A button "Open ticket" and a link "Help center"; the message '
                      '"Ticket title is required"; for example email "bad-address" or password '
                      '"Sample-pass-42!".', "The seeded data is account `sam-dev` and `kim-dev`.")]
        with tempfile.TemporaryDirectory() as directory:
            root = app(Path(directory), "<button>Open ticket</button><a>Help center</a>",
                       "users.push({ username: 'sam-dev' })")
            notes = contract_notes(root, nodes)
        self.assertEqual(notes, ['- name "Ticket title is required" is not rendered anywhere',
                                 "- seed record `kim-dev` is not created by the backend"])

    def test_data_holding_the_only_copy_of_a_seed_must_be_kept(self):
        nodes = [node("", "The seeded board `Weekly Plan` exists.")]
        with tempfile.TemporaryDirectory() as directory:
            root = app(Path(directory), "", "module.exports = {}",
                       '{"boards": [{"name": "Weekly Plan"}]}')
            self.assertEqual(seeds_only_in_data(root, nodes), ["Weekly Plan"])
            (root / "backend" / "src" / "seed.js").write_text("const name = 'Weekly Plan'",
                                                              encoding="utf-8")
            self.assertEqual(seeds_only_in_data(root, nodes), [])

    def test_a_bare_number_in_the_data_does_not_keep_polluted_data(self):
        nodes = [node("", "Cell B2 of the seeded board `Weekly Plan` holds `13`.")]
        with tempfile.TemporaryDirectory() as directory:
            root = app(Path(directory), "", "const name = 'Weekly Plan'",
                       '{"boards": [{"name": "Weekly Plan", "cells": {"B2": "130"}}]}')
            self.assertEqual(seeds_only_in_data(root, nodes), [])


if __name__ == "__main__":
    unittest.main()
