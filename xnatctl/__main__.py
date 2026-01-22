"""Allow running xnatctl as a module: python -m xnatctl."""

from xnatctl.cli.main import main

if __name__ == "__main__":
    main()
