"""Create the six persistent tables without overwriting existing records."""

from src.config import Settings
from src.data.repository import Repository

if __name__ == "__main__":
    repo = Repository(Settings().database_url)
    print("Database ready:", repo.engine.url.render_as_string(hide_password=True))
