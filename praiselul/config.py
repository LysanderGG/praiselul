import configparser
import os
import os.path
from dataclasses import dataclass

DEFAULT_CONFIG_DIR = os.path.expanduser("~/.praiselul")
DEFAULT_CONFIG_PATH = os.path.join(DEFAULT_CONFIG_DIR, "config.ini")

DEFAULT_HOURS_PER_DAY = 8


@dataclass
class Config:
    praise_url: str
    praise_email: str
    praise_password: str
    hours_per_day: int = DEFAULT_HOURS_PER_DAY

    @classmethod
    def from_env(cls):
        try:
            return cls(
                praise_url=os.environ["PRAISE_URL"],
                praise_email=os.environ["PRAISE_EMAIL"],
                praise_password=os.environ["PRAISE_PASSWORD"],
                hours_per_day=int(os.getenv("PRAISE_HOURS_PER_DAY", DEFAULT_HOURS_PER_DAY)),
            )
        except KeyError:
            return None

    @classmethod
    def load(cls, path: str = DEFAULT_CONFIG_PATH):
        if not os.path.isfile(path):
            return None

        config = configparser.ConfigParser(interpolation=None)
        config.read(path)
        return cls(
            praise_url=config["praise"]["url"],
            praise_email=config["praise"]["email"],
            praise_password=config["praise"]["password"],
            hours_per_day=int(config["praise"].get("hoursPerDay", str(DEFAULT_HOURS_PER_DAY))),
        )

    def save(self, path: str = DEFAULT_CONFIG_PATH):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        config = configparser.ConfigParser(interpolation=None)
        config["praise"] = {
            "url": self.praise_url,
            "email": self.praise_email,
            "password": self.praise_password,
            "hoursPerDay": str(self.hours_per_day),
        }
        with open(path, "w") as config_file:
            config.write(config_file)
