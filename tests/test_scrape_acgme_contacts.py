"""Tests for the ACGME contact scraper parsing and export helpers."""

import csv
import json
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

import logger
import scrape as scraper
from logger import CLI_THEME, print_banner, print_run_config
from checkpoint_to_csv import convert_checkpoint_to_csv
from rich.console import Console
python scrape_acgme_contacts import (
    ContactRow,
    ProgramResult,
    append_rows_to_checkpoint,
    empty_checkpoint,
    mark_program_completed,
    parse_contact_rows,
    parse_program_results,
    parse_state_options,
    record_error,
    select_states,
)


"""# --- Test fixture helpers ---"""


def make_program(
        code: str = "0200500131",
        name: str = "University of California (San Francisco) School of Medicine Program",
) -> ProgramResult:
    """Create a sample program result for parser tests."""
    return ProgramResult(
        program_code=code,
        program_name=name,
        specialty="Allergy and immunology",
        state="California",
        city="San Francisco",
        detail_url=f"https://apps.acgme.org/ads/Public/Programs/Detail?orgCode={code}",
    )


def make_contact(
        role: str = "Program Coordinator",
        code: str = "0200500131",
        email: str = "jane@example.edu",
        name: str = "Jane Doe",
) -> ContactRow:
    """Create a sample contact row for checkpoint tests."""
    program = make_program(code=code, name=f"Program {code}")
    return ContactRow(
        program_code=program.program_code,
        program_name=program.program_name,
        specialty=program.specialty,
        state=program.state,
        city=program.city,
        role=role,
        name=name,
        email=email,
        phone="(555) 555-1212",
        source_url=program.detail_url,
    )


"""# --- Parser test cases ---"""


class ParserTests(unittest.TestCase):
    """Parser tests for saved and synthetic ACGME-like HTML."""

    def test_parse_state_options_skips_prompt(
            self,
    ) -> None:
        """State parsing returns only real state or territory options."""
        html = """
        <select id="stateFilter" name="stateId">
          <option value="">Search by State</option>
          <option value="5">California</option>
          <option value="43">Texas</option>
          <option value="52">Puerto Rico</option>
        </select>
        """

        states = parse_state_options(html)

        self.assertEqual([state.name for state in states], ["California", "Texas", "Puerto Rico"])
        self.assertEqual([state.code for state in states], ["5", "43", "52"])

    def test_select_states_accepts_abbreviation_name_and_code(
            self,
    ) -> None:
        """State selection accepts mixed abbreviations, names, and ACGME state IDs."""
        states = [
            scraper.StateOption(code="5", name="California"),
            scraper.StateOption(code="43", name="Texas"),
            scraper.StateOption(code="52", name="Puerto Rico"),
        ]

        selected = select_states(states, ["CA", "Texas", "52"])

        self.assertEqual([state.name for state in selected], ["California", "Texas", "Puerto Rico"])

    def test_parse_program_results_extracts_detail_links(
            self,
    ) -> None:
        """Program result parsing extracts code, metadata, and detail URL."""
        html = """
        <table id="programsListView-listview">
          <tr><th></th><th>Code</th><th>Specialty</th><th>Name</th><th>City</th><th></th><th></th></tr>
          <tr>
            <td>,1,9,</td>
            <td>0200500131</td>
            <td>Allergy and immunology</td>
            <td>University of California (San Francisco) School of Medicine Program</td>
            <td>San Francisco</td>
            <td></td>
            <td><a href="/ads/Public/Programs/Detail?orgCode=0200500131">View Program</a></td>
          </tr>
        </table>
        """

        programs = parse_program_results(html, "California")

        self.assertEqual(len(programs), 1)
        self.assertEqual(programs[0].program_code, "0200500131")
        self.assertEqual(programs[0].specialty, "Allergy and immunology")
        self.assertEqual(programs[0].city, "San Francisco")
        self.assertTrue(programs[0].detail_url.endswith("orgCode=0200500131"))

    def test_parse_program_results_handles_empty_results(
            self,
    ) -> None:
        """Program result parsing returns an empty list for no-result tables."""
        html = """
        <table>
          <tr><th></th><th>Code</th><th>Specialty</th><th>Name</th><th>City</th><th></th><th></th></tr>
          <tr><td colspan="7">No Programs found for the input and/or selected search criteria.</td></tr>
        </table>
        """

        self.assertEqual(parse_program_results(html, "California"), [])

    def test_parse_contact_rows_extracts_contacts_and_blanks(
            self,
    ) -> None:
        """Contact parsing extracts leadership rows and leaves missing values blank."""
        html = """
        <div>
          <div class="panel panel-default">
            <div class="panel-heading d-grid">
              <h3 class="panel-title">Director Information</h3>
            </div>
          </div>
        </div>
        <div>
          <ul class="list-unstyled">
            <li>Rose Monahan, MD</li>
            <li>Adult Program Director</li>
          </ul>
          <dl class="inline">
            <dt>Director First Appointed:</dt><dd>May 19, 2026</dd>
          </dl>
        </div>
        <div>
          <div class="panel panel-default">
            <div class="panel-heading d-grid">
              <h3 class="panel-title">Coordinator Information</h3>
            </div>
          </div>
          <ul class="list-unstyled">
            <li>Mrs. Twinkle T Patel, MPA</li>
            <li>Program Coordinator</li>
          </ul>
          <dl class="inline">
            <dt>Phone: </dt><dd>(415) 502-2067</dd>
            <dt>Email: </dt><dd><a href="mailto:twinkle.patel@ucsf.edu">twinkle.patel@ucsf.edu</a></dd>
          </dl>
        </div>
        """

        rows = parse_contact_rows(html, make_program())

        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0].name, "Rose Monahan, MD")
        self.assertEqual(rows[0].role, "Adult Program Director")
        self.assertEqual(rows[0].email, "")
        self.assertEqual(rows[0].phone, "")
        self.assertEqual(rows[1].name, "Mrs. Twinkle T Patel, MPA")
        self.assertEqual(rows[1].email, "twinkle.patel@ucsf.edu")
        self.assertEqual(rows[1].phone, "(415) 502-2067")

    def test_parse_contact_rows_preserves_same_person_with_different_roles(
            self,
    ) -> None:
        """Contact parsing keeps duplicate people when their role differs."""
        html = """
        <div>
          <div class="panel panel-default">
            <div class="panel-heading"><h3>Coordinator Information</h3></div>
          </div>
          <ul class="list-unstyled"><li>Jane Doe</li><li>Program Coordinator</li></ul>
          <dl><dt>Email:</dt><dd><a href="mailto:jane@example.edu">jane@example.edu</a></dd></dl>
        </div>
        <div>
          <div class="panel panel-default">
            <div class="panel-heading"><h3>Coordinator Information</h3></div>
          </div>
          <ul class="list-unstyled"><li>Jane Doe</li><li>Program Manager</li></ul>
          <dl><dt>Email:</dt><dd><a href="mailto:jane@example.edu">jane@example.edu</a></dd></dl>
        </div>
        """

        rows = parse_contact_rows(html, make_program())

        self.assertEqual(len(rows), 2)
        self.assertEqual([row.role for row in rows], ["Program Coordinator", "Program Manager"])

    def test_parse_contact_rows_uses_clean_section_role_when_role_is_blank(
            self,
    ) -> None:
        """Contact parsing uses a clean section role when ACGME leaves role text empty."""
        html = """
        <div>
          <div class="panel panel-default">
            <div class="panel-heading"><h3>Coordinator Information</h3></div>
          </div>
          <ul class="list-unstyled"><li>Jane Doe</li><li></li></ul>
          <dl><dt>Email:</dt><dd><a href="mailto:jane@example.edu">jane@example.edu</a></dd></dl>
        </div>
        """

        rows = parse_contact_rows(html, make_program())

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].role, "Coordinator")

    def test_parse_contact_rows_excludes_non_contact_sections(
            self,
    ) -> None:
        """Contact parsing excludes faculty or site sections without contact headings."""
        html = """
        <div>
          <div class="panel-heading"><h3>Faculty Information</h3></div>
          <ul class="list-unstyled"><li>Faculty Person</li><li>Faculty</li></ul>
        </div>
        <div>
          <div class="panel-heading"><h3>Participating Site Information</h3></div>
          <ul class="list-unstyled"><li>UCSF Health</li><li>Site</li></ul>
        </div>
        """

        self.assertEqual(parse_contact_rows(html, make_program()), [])


"""# --- Checkpoint test cases ---"""


class CheckpointTests(unittest.TestCase):
    """Checkpoint tests for resumable scraper behavior."""

    def test_checkpoint_appends_raw_rows_and_records_completion(
            self,
    ) -> None:
        """Checkpoint helpers keep raw rows and preserve completed program codes."""
        checkpoint = empty_checkpoint()
        row = make_contact()

        append_rows_to_checkpoint(checkpoint, [row, row])
        mark_program_completed(checkpoint, row.program_code)
        record_error(checkpoint, make_program(), "program detail", "example failure")

        self.assertEqual(len(checkpoint["rows"]), 2)
        self.assertEqual(checkpoint["completed_program_codes"], [row.program_code])
        self.assertEqual(len(checkpoint["errors"]), 1)
        self.assertEqual(checkpoint["errors"][0]["stage"], "program detail")

    def test_checkpoint_keeps_same_email_rows_raw(
            self,
    ) -> None:
        """Checkpoint helpers do not merge multiple roles under one email identity."""
        checkpoint = empty_checkpoint()

        append_rows_to_checkpoint(
            checkpoint,
            [
                make_contact(role="Program Coordinator"),
                make_contact(role="Program Manager"),
            ],
        )

        self.assertEqual(len(checkpoint["rows"]), 2)
        self.assertEqual(
            [row["Role"] for row in checkpoint["rows"]],
            ["Program Coordinator", "Program Manager"],
        )

    def test_checkpoint_keeps_same_email_rows_raw_across_programs(
            self,
    ) -> None:
        """Checkpoint helpers do not use email identity across different programs."""
        checkpoint = empty_checkpoint()

        append_rows_to_checkpoint(
            checkpoint,
            [
                make_contact(code="0200500131", role="Program Coordinator"),
                make_contact(code="0200521048", role="Program Manager"),
            ],
        )

        self.assertEqual(len(checkpoint["rows"]), 2)
        self.assertEqual(
            [row["Program Code"] for row in checkpoint["rows"]],
            ["0200500131", "0200521048"],
        )
        self.assertEqual(
            [row["Role"] for row in checkpoint["rows"]],
            ["Program Coordinator", "Program Manager"],
        )

    def test_checkpoint_does_not_merge_blank_email_across_programs(
            self,
    ) -> None:
        """Checkpoint helpers keep blank-email contacts separate by program and role."""
        checkpoint = empty_checkpoint()

        append_rows_to_checkpoint(
            checkpoint,
            [
                make_contact(code="0200500131", email="", name="Jane Doe"),
                make_contact(code="0200521048", email="", name="Jane Doe"),
            ],
        )

        self.assertEqual(len(checkpoint["rows"]), 2)
        self.assertEqual(
            [row["Program Code"] for row in checkpoint["rows"]],
            ["0200500131", "0200521048"],
        )


"""# --- CSV export test cases ---"""


class CsvExportTests(unittest.TestCase):
    """CSV export tests for checkpoint-to-CSV conversion."""

    def test_convert_checkpoint_to_csv_writes_expected_columns(
            self,
    ) -> None:
        """Checkpoint conversion writes the final CSV with the configured schema."""
        checkpoint = empty_checkpoint()
        append_rows_to_checkpoint(checkpoint, [make_contact()])

        with tempfile.TemporaryDirectory() as temporary_directory:
            checkpoint_path = Path(temporary_directory) / "checkpoint.json"
            output_path = Path(temporary_directory) / "contacts.csv"
            checkpoint_path.write_text(
                json.dumps(checkpoint),
                encoding="utf-8",
            )

            row_count = convert_checkpoint_to_csv(
                checkpoint_path,
                output_path,
            )

            with output_path.open("r", encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))

        self.assertEqual(row_count, 1)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["Program Code"], "0200500131")
        self.assertEqual(rows[0]["Role"], "Program Coordinator")

    def test_convert_checkpoint_to_csv_dedupes_existing_checkpoint_rows(
            self,
    ) -> None:
        """Checkpoint conversion dedupes old raw rows before writing CSV."""
        first = make_contact(code="0200500131", role="Program Coordinator")
        second = make_contact(code="0200521048", role="Program Manager")
        checkpoint = {
            "rows": [
                scraper.contact_row_to_checkpoint_dict(first),
                scraper.contact_row_to_checkpoint_dict(second),
            ],
        }

        with tempfile.TemporaryDirectory() as temporary_directory:
            checkpoint_path = Path(temporary_directory) / "checkpoint.json"
            output_path = Path(temporary_directory) / "contacts.csv"
            checkpoint_path.write_text(
                json.dumps(checkpoint),
                encoding="utf-8",
            )

            row_count = convert_checkpoint_to_csv(
                checkpoint_path,
                output_path,
            )

            with output_path.open("r", encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))

        self.assertEqual(row_count, 1)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["Program Code"], "0200500131; 0200521048")
        self.assertEqual(rows[0]["Role"], "Program Coordinator; Program Manager")

    def test_convert_checkpoint_to_csv_merges_roles_for_same_email(
            self,
    ) -> None:
        """Checkpoint conversion merges multiple roles under one email identity."""
        checkpoint = empty_checkpoint()
        append_rows_to_checkpoint(
            checkpoint,
            [
                make_contact(role="Program Coordinator"),
                make_contact(role="Program Manager"),
            ],
        )

        with tempfile.TemporaryDirectory() as temporary_directory:
            checkpoint_path = Path(temporary_directory) / "checkpoint.json"
            output_path = Path(temporary_directory) / "contacts.csv"
            checkpoint_path.write_text(
                json.dumps(checkpoint),
                encoding="utf-8",
            )

            row_count = convert_checkpoint_to_csv(
                checkpoint_path,
                output_path,
            )

            with output_path.open("r", encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))

        self.assertEqual(row_count, 1)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["Role"], "Program Coordinator; Program Manager")

    def test_convert_checkpoint_to_csv_keeps_blank_email_rows_separate(
            self,
    ) -> None:
        """Checkpoint conversion does not globally merge contacts without email."""
        checkpoint = empty_checkpoint()
        append_rows_to_checkpoint(
            checkpoint,
            [
                make_contact(code="0200500131", email="", name="Jane Doe"),
                make_contact(code="0200521048", email="", name="Jane Doe"),
            ],
        )

        with tempfile.TemporaryDirectory() as temporary_directory:
            checkpoint_path = Path(temporary_directory) / "checkpoint.json"
            output_path = Path(temporary_directory) / "contacts.csv"
            checkpoint_path.write_text(
                json.dumps(checkpoint),
                encoding="utf-8",
            )

            row_count = convert_checkpoint_to_csv(
                checkpoint_path,
                output_path,
            )

            with output_path.open("r", encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))

        self.assertEqual(row_count, 2)
        self.assertEqual(
            [row["Program Code"] for row in rows],
            ["0200500131", "0200521048"],
        )


"""# --- Workflow test cases ---"""


class WorkflowTests(unittest.TestCase):
    """Workflow tests for scraper orchestration."""

    def test_run_scrape_reuses_detail_fetcher_and_skips_completed_programs(
            self,
    ) -> None:
        """Scraper reuses one detail fetcher and does not fetch skipped details."""
        programs = [
            make_program(code="0000000001", name="Completed Program"),
            make_program(code="0000000002", name="New Program"),
        ]
        instances = []

        class FakeDetailFetcher:
            """Fake detail fetcher that records detail page requests."""

            def __init__(
                    self,
                    delay: float,
            ) -> None:
                """Initialize the fake fetcher."""
                self.delay = delay
                self.fetches = []
                self.closed = False
                instances.append(self)

            def __enter__(
                    self,
            ) -> "FakeDetailFetcher":
                """Enter the fake context manager."""
                return self

            def __exit__(
                    self,
                    exc_type,
                    exc_value,
                    traceback,
            ) -> None:
                """Record that the fake context manager closed."""
                self.closed = True

            def fetch_detail_html(
                    self,
                    program: ProgramResult,
            ) -> str:
                """Return synthetic detail HTML for one program."""
                self.fetches.append(program.program_code)
                return """
                <div>
                  <div class="panel panel-default">
                    <div class="panel-heading"><h3>Coordinator Information</h3></div>
                  </div>
                  <ul class="list-unstyled"><li>Jane Doe</li><li>Program Coordinator</li></ul>
                  <dl>
                    <dt>Email:</dt><dd><a href="mailto:jane@example.edu">jane@example.edu</a></dd>
                  </dl>
                </div>
                """

        def fake_get_search_page(
                session,
                delay,
        ) -> str:
            """Return a minimal search page fixture."""
            return """
            <input name="__RequestVerificationToken" value="token" />
            <select id="stateFilter"><option value="5">California</option></select>
            """

        def fake_search_programs_for_state(
                session,
                state,
                token,
                delay,
        ):
            """Return synthetic state search results."""
            return programs

        originals = {
            "get_search_page": scraper.get_search_page,
            "search_programs_for_state": scraper.search_programs_for_state,
            "PlaywrightDetailFetcher": scraper.PlaywrightDetailFetcher,
        }
        scraper.get_search_page = fake_get_search_page
        scraper.search_programs_for_state = fake_search_programs_for_state
        scraper.PlaywrightDetailFetcher = FakeDetailFetcher
        original_console = logger.CONSOLE
        logger.CONSOLE = Console(
            file=StringIO(),
            force_terminal=False,
            color_system=None,
            theme=CLI_THEME,
            width=120,
        )
        try:
            with tempfile.TemporaryDirectory() as temporary_directory:
                checkpoint_path = Path(temporary_directory) / "checkpoint.json"
                checkpoint_path.write_text(
                    json.dumps(
                        {
                            "created_at": "2026-05-25T00:00:00+00:00",
                            "updated_at": "2026-05-25T00:00:00+00:00",
                            "completed_program_codes": ["0000000001"],
                            "rows": [],
                            "errors": [],
                        }
                    ),
                    encoding="utf-8",
                )
                args = SimpleNamespace(
                    checkpoint=str(checkpoint_path),
                    states=["CA"],
                    delay=0.0,
                    force=False,
                    max_programs=None,
                )

                scraper.run_scrape(args)
                checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        finally:
            scraper.get_search_page = originals["get_search_page"]
            scraper.search_programs_for_state = originals["search_programs_for_state"]
            scraper.PlaywrightDetailFetcher = originals["PlaywrightDetailFetcher"]
            logger.CONSOLE = original_console

        self.assertEqual(len(instances), 1)
        self.assertEqual(instances[0].fetches, ["0000000002"])
        self.assertTrue(instances[0].closed)
        self.assertEqual(checkpoint["completed_program_codes"], ["0000000001", "0000000002"])
        self.assertEqual(len(checkpoint["rows"]), 1)


"""# --- CLI output test cases ---"""


class CliOutputTests(unittest.TestCase):
    """CLI output tests for shared Rich formatting helpers."""

    def test_rich_banner_and_config_render_plain_text(
            self,
    ) -> None:
        """Rich helpers render banner and configuration text under test capture."""
        output = StringIO()
        console = Console(
            file=output,
            force_terminal=False,
            color_system=None,
            theme=CLI_THEME,
            width=100,
        )

        print_banner(
            "ACGME CONTACTS",
            console_obj=console,
        )
        print_run_config(
            "Scrape configuration",
            [
                ("Checkpoint", "data/acgme_checkpoint.json"),
                ("States", "California"),
            ],
            console_obj=console,
        )

        rendered = output.getvalue()
        self.assertIn("ACGME CONTACTS", rendered)
        self.assertIn("Scrape configuration", rendered)
        self.assertIn("data/acgme_checkpoint.json", rendered)


if __name__ == "__main__":
    unittest.main()
