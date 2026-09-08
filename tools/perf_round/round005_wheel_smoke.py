"""Run with an isolated venv Python -I after installing the candidate wheel."""
from importlib.metadata import version
from pathlib import Path
from tempfile import TemporaryDirectory

import okto_grafx
from okto_grafx import ConnectOptions, connect


def main():
    assert version("okto-grafx") == okto_grafx.__version__ == "0.0.5"
    source = Path(__file__).resolve().parents[2] / "src"
    assert not Path(okto_grafx.__file__).resolve().is_relative_to(source)
    with TemporaryDirectory(prefix="grafx-v005-wheel-") as raw:
        options: ConnectOptions = {"descriptor_revalidation": "strict"}
        assert len(ConnectOptions.__annotations__) == 35
        with connect(raw, **options) as db:
            with db.begin("write") as tx:
                tx.execute("CREATE NODE TABLE Item(id INT64, PRIMARY KEY(id))")
            with db.begin("write") as tx:
                for i in range(4):
                    tx.execute("CREATE (:Item {id:$id})", {"id": i})
            for _ in range(2):
                for shape in range(160):
                    assert db.execute(f"RETURN $v IN $ids AS value_{shape}",
                                      {"v": 3.0, "ids": [1, 2, 3, None]}).rows == ((True,),)
            assert db.execute("MATCH (n:Item) RETURN n.id ORDER BY n.id DESC LIMIT 2").rows == ((3,), (2,))
            db.checkpoint()
        with connect(raw) as reopened:
            assert reopened.execute("MATCH (n:Item) RETURN count(n)").rows == ((4,),)
            assert reopened.verify("all").findings == ()
    print(f"Installed wheel smoke passed: {okto_grafx.__version__} / {okto_grafx.__file__}")


if __name__ == "__main__":
    main()
