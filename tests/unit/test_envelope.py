"""Unit tests for the shared tool envelope helper (task 1).

Covers the structured ``{status, message, data}`` shape and the
OK/EMPTY/ERROR status constants used by both MCP servers.
"""

import pytest

from shared import (
    STATUS_EMPTY,
    STATUS_ERROR,
    STATUS_OK,
    Status,
    ToolEnvelope,
    empty,
    error,
    ok,
)


class TestStatusConstants:
    def test_string_constants_match_enum(self):
        assert STATUS_OK == Status.OK.value == "OK"
        assert STATUS_EMPTY == Status.EMPTY.value == "EMPTY"
        assert STATUS_ERROR == Status.ERROR.value == "ERROR"

    def test_status_enum_compares_equal_to_plain_string(self):
        # Status subclasses str, so this comparison must hold.
        assert Status.OK == "OK"
        assert Status.EMPTY == "EMPTY"
        assert Status.ERROR == "ERROR"


class TestOkFactory:
    def test_ok_envelope_shape(self):
        result = ok([{"id": 1}], message="found it")
        assert result == {
            "status": "OK",
            "message": "found it",
            "data": [{"id": 1}],
        }

    def test_ok_defaults_to_empty_list_and_message(self):
        result = ok()
        assert result["status"] == STATUS_OK
        assert result["message"] == ""
        assert result["data"] == []

    def test_ok_materializes_generator_data(self):
        result = ok((x for x in range(3)))
        assert result["data"] == [0, 1, 2]

    def test_ok_wraps_single_dict_as_one_record(self):
        # A raw dict is a single record, not an iterable of keys.
        result = ok({"ndc": "0001-0001"})
        assert result["data"] == [{"ndc": "0001-0001"}]


class TestEmptyFactory:
    def test_empty_envelope_shape(self):
        result = empty("no records found for amoxicillin")
        assert result["status"] == STATUS_EMPTY
        assert result["data"] == []
        assert "no records found" in result["message"]


class TestErrorFactory:
    def test_error_envelope_shape(self):
        result = error("search_drug_shortages failed: openFDA 500")
        assert result["status"] == STATUS_ERROR
        assert result["data"] == []
        assert "search_drug_shortages" in result["message"]


class TestToolEnvelope:
    def test_accepts_plain_string_status(self):
        env = ToolEnvelope(status="OK", message="hi", data=[1])
        assert env.status is Status.OK
        assert env.is_ok and not env.is_empty and not env.is_error

    def test_none_data_coerced_to_list(self):
        env = ToolEnvelope(status=Status.EMPTY, message="none", data=None)
        assert env.data == []

    def test_predicates(self):
        assert ToolEnvelope(Status.EMPTY).is_empty
        assert ToolEnvelope(Status.ERROR).is_error

    def test_round_trip_dict(self):
        original = ToolEnvelope(Status.OK, "msg", [{"a": 1}])
        rebuilt = ToolEnvelope.from_dict(original.to_dict())
        assert rebuilt.to_dict() == original.to_dict()

    def test_invalid_status_raises(self):
        with pytest.raises(ValueError):
            ToolEnvelope(status="NOPE")
