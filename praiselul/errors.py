class CliLoginError(Exception):
    """The browser-approved CLI login could not be completed."""


class CliLoginExpiredError(CliLoginError):
    """The login request expired or its code was already used."""
    def __init__(self):
        super().__init__("Login request expired or the code was invalid. Run the command again.")


class CliLoginDeniedError(CliLoginError):
    """The login request was denied in the browser."""
    def __init__(self):
        super().__init__("Login was denied in the browser.")


class NoClockInError(Exception):
    """A clock-in time was expected but wasn't found"""
