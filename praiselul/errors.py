class InvalidPraiseLoginError(Exception):
    """Invalid Praise login information"""
    def __init__(self):
        super().__init__("Invalid Praise login information. Run praiselul config")


class NoClockInError(Exception):
    """A clock-in time was expected but wasn't found"""
