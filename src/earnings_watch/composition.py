from .service import PersonService
from .settings import Settings
from .store.database import LibraryReader, PersonalStore


def open_service(settings: Settings, person: str = "local") -> PersonService:
    return PersonService(
        LibraryReader(settings.db),
        PersonalStore(settings.db, person),
        (settings.data_dir / "calendar.key").read_bytes(),
    )
