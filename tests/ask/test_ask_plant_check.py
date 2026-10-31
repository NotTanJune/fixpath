"""Tests of the plant check, which catches a question that names a production line the
plant does not have. The test graph is the small crew graph, which has one line.
"""

import pytest
from crew_stubs import open_crew_kg
from llm_stubs import isolated  # noqa: F401  (an autouse fixture)

from app.ask.plant_check import exact_asset_ids, named_lines, plant_lines, unknown_line


@pytest.fixture
def conn(tmp_path):
    connection = open_crew_kg(tmp_path)
    yield connection
    connection.close()


def test_the_plant_lines_are_the_numbers_of_the_line_nodes(conn):
    assert plant_lines(conn) == {1}


@pytest.mark.parametrize(
    ("question", "named"),
    [
        ("Line 4 robot is down", [4]),
        ("the robot on line 12 stopped", [12]),
        ("LINE 3 is down and Line 4 too", [3, 4]),
        ("HP-01 on Line 4 leaking", [4]),
        ("line 07 press is down", [7]),
        ("Line-4 robot is down", [4]),
        ("line4 robot is down", [4]),
        ("line four robot is down", [4]),
        ("the Line Twelve press", [12]),
        # a program word elsewhere in the sentence does not hide a plant line
        ("Line 4 robot error code 12", [4]),
        ("line 4 press fault code E-217", [4]),
        ("Line 6 CNC-01 stops at program start", [6]),
        ("Line 4 CNC program stopped", [4]),
        ("line 4 press, nc alarm", [4]),
        ("robot on line 4 stopped. See the log", [4]),
        ("See the log. Line 4 robot is down", [4]),
        ("the robot stopped at line 4", [4]),
        ("HP-01 down at line 4", [4]),
        ("The program ended. The robot stopped at line 4", [4]),
        ("Line 5 press leaking oil", [5]),
        ("line 4 robot is down", [4]),
        ("Line 4, robot down", [4]),
        ("line 4 CNC-01 is down", [4]),
        ("Line 4 in the plant is down", [4]),
        ("Line 3 robot is down", [3]),
        # a position in a program, a file or a document is not a plant line
        ("alarm on line 120 of the program on CNC-01", []),
        ("line 12 of the log says E-217", []),
        ("line 4 of the log", []),
        ("it stops on line 40 of the file", []),
        ("on line 40 of the file", []),
        ("program line 12 stops", []),
        ("log line 12 shows E-217", []),
        ("G-code line 15 has a bad move", []),
        ("see line 12 in the spreadsheet", []),
        ("see line 12 of page 3", []),
        ("the NC program stops at line 40", []),
        ("the macro fails at line 7", []),
        ("the program stopped, then the alarm came at line 12", []),
        ("see page 3 line 12 of the manual", []),
        ("sheet 2 line 5", []),
        # a number with a unit right after it is a measurement
        ("main line 2 bar drop on HP-01", []),
        ("main line 2bar drop on HP-01", []),
        ("line 6 bar too low on HP-01", []),
        ("supply line 3mm crack", []),
        ("Return line 40 PSI at the press", []),
        ("oil line 12% blocked", []),
        ("coolant line 5 lpm", []),
        ("page three line 12", []),
        ("the error is in the program, line 8", []),
        ("line four of the program", []),
        # a number that is not one or two digits is no line
        ("line 120 is wrong", []),
        ("line 1234", []),
        ("no line here", []),
        ("CNC-01 line feed alarm", []),
        ("line speed is low on HP-01", []),
        ("outline 4 and guideline 5", []),
    ],
)
def test_named_lines_skips_only_a_program_word_next_to_the_line_number(question, named):
    assert named_lines(question) == named


def test_unknown_line_is_the_first_named_line_the_plant_does_not_have(conn):
    assert unknown_line("Line 4 robot is down", conn) == 4
    assert unknown_line("Line 1 robot is down", conn) is None
    assert unknown_line("Line 1 and line 2 are down", conn) == 2
    assert unknown_line("alarm on line 120 of the program on CNC-01", conn) is None
    assert unknown_line("the press is down", conn) is None


def test_exact_asset_ids_are_the_ids_written_in_the_text(conn):
    assert exact_asset_ids("hp-01 on Line 4 leaking", conn) == ["HP-01"]
    assert exact_asset_ids("CNC-01 and CNC-02 both stopped", conn) == [
        "CNC-01",
        "CNC-02",
    ]
    assert exact_asset_ids("the press and the robot", conn) == []  # nicknames are not ids
    assert exact_asset_ids("KX-500 is down", conn) == []  # a model name is not an id
