"""CSV cells that a spreadsheet will never run as a formula."""

_FORMULA_START = ('=', '+', '-', '@', '\t', '\r')


def csv_cell(value):
    """Text for a CSV cell; a leading =, +, -, @, tab or CR is quoted with '
    so Excel / Sheets treat the cell as text."""
    text = '' if value is None else str(value)
    return "'" + text if text.startswith(_FORMULA_START) else text
