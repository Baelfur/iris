"""Common exception types for the service layer — decouples routes from native driver errors."""


class DatabaseError(Exception):
    """Raised by variant db layer to wrap native driver exceptions."""
