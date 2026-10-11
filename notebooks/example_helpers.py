"""Offline helpers. Importing this module does not create any clients or requests."""
import json


def load_example(root, name):
    return json.loads((root / "notebooks" / "examples" / f"{name}.json").read_text())


def expect_failure(operation, contains=None):
    try:
        operation()
    except (ValueError, TypeError) as error:
        if contains is not None:
            assert contains in str(error), f"Unexpected failure: {error}"
        print(f"Expected {type(error).__name__}:\n{error}")
        return error
    raise AssertionError("Expected validation to reject this example, but it passed")
