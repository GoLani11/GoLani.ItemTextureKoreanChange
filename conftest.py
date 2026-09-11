def pytest_addoption(parser):
    parser.addoption("--spt-root", default=None, help="Read-only SPT root for opt-in game asset tests")
    parser.addoption("--dotnet", default="dotnet", help=".NET 10 executable for game asset packaging tests")
