# Importing models here guarantees every ORM model is registered on
# Base.metadata as soon as anything imports the database package —
# regardless of which submodule (base, models) the caller imported
# directly. Without this, init_db()'s create_all() can silently
# create zero tables if the caller never happened to import models.
from naukri_agent.database import models  # noqa: F401
