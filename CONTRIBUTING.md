# Contributing Guidelines

[Project home](README.md) · [Deployment guide](docs/deployment.md)

Thank you for your interest in contributing to our project. Whether it's a bug report, new feature, correction, or additional
documentation, we greatly value feedback and contributions from our community.

Please read through this document before submitting any issues or pull requests to ensure we have all the necessary
information to effectively respond to your bug report or contribution.


## Reporting Bugs/Feature Requests

We welcome you to use the GitHub issue tracker to report bugs or suggest features.

When filing an issue, please check existing open, or recently closed, issues to make sure somebody else hasn't already
reported the issue. Please try to include as much information as you can. Details like these are incredibly useful:

* A reproducible test case or series of steps
* The version of our code being used
* Any modifications you've made relevant to the bug
* Anything unusual about your environment or deployment


## Contributing via Pull Requests
Contributions via pull requests are much appreciated. Before sending us a pull request, please ensure that:

1. You are working against the latest source on the *main* branch.
2. You check existing open, and recently merged, pull requests to make sure someone else hasn't addressed the problem already.
3. You open an issue to discuss any significant work - we would hate for your time to be wasted.

To send us a pull request, please:

1. Fork the repository.
2. Modify the source; please focus on the specific change you are contributing. If you also reformat all the code, it will be hard for us to focus on your change.
3. Ensure local tests pass.
4. Commit to your fork using clear commit messages.
5. Send us a pull request, answering any default questions in the pull request interface.
6. Pay attention to any automated CI failures reported in the pull request, and stay involved in the conversation.

GitHub provides additional document on [forking a repository](https://help.github.com/articles/fork-a-repo/) and
[creating a pull request](https://help.github.com/articles/creating-a-pull-request/).


## Finding contributions to work on
Looking at the existing issues is a great way to find something to contribute on. As our projects, by default, use the default GitHub issue labels (enhancement/bug/duplicate/help wanted/invalid/question/wontfix), looking at any 'help wanted' issues is a great place to start.


## Local development

Use Python 3.12, Node.js/npm, and Docker Compose. From the repository root,
prepare a virtual environment and the backend/test dependencies:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r backend/requirements.txt pytest pytest-asyncio httpx
if [[ ! -e config.yaml ]]; then
  cp config.example.yaml config.yaml
fi
```

Start the local services. Compose initializes the database schema on a new
Postgres volume; apply the partition setup from the repository root:

```bash
docker compose up -d
docker compose exec -T postgres psql -U bedrock_lens -d bedrock_lens < db/partitions.sql
```

Wait for Postgres to be healthy before running the SQL command. Start the
backend in this terminal:

```bash
cd backend
DATABASE_URL=postgresql://bedrock_lens:bedrock_lens_dev@localhost:5432/bedrock_lens \
  AUTH_ENABLED=false PYTHONPATH=.. uvicorn app.main:app --port 8001
```

The database credentials above are the local Compose defaults. The
`AUTH_ENABLED=false` setting is for this local development server.

In a separate terminal, start from the repository root:

```bash
cd frontend
npm install
npm run dev
```

The frontend is at http://localhost:5173. It uses the same FastAPI application
and ingestion code as the Lambda deployment.

## Tests

From the repository root with the Python environment activated:

```bash
python -m pytest -q
```

The suite covers CloudWatch accounting, quota matching and burndown, cache-token
accounting, telemetry payloads, ingestion outcomes, and deployment/onboarding
behavior. Most tests use local fakes. API-dependent tests need a running backend;
report skips and exclusions separately from passing tests.

With the local backend running as described above:

```bash
LENS_API=http://localhost:8001/api python -m pytest -q
```

For deployed UI checks, see the [Playwright instructions](docs/deployment.md#verify).
For quota drill-down internals, see the
[implementation reference](docs/quota-drilldown-implementation.md).

## Code of Conduct
This project has adopted the [Amazon Open Source Code of Conduct](https://aws.github.io/code-of-conduct).
For more information see the [Code of Conduct FAQ](https://aws.github.io/code-of-conduct-faq) or contact
opensource-codeofconduct@amazon.com with any additional questions or comments.


## Security issue notifications
If you discover a potential security issue in this project we ask that you notify AWS/Amazon Security via our [vulnerability reporting page](http://aws.amazon.com/security/vulnerability-reporting/). Please do **not** create a public github issue.


## Licensing

See the [LICENSE](LICENSE) file for our project's licensing. We will ask you to confirm the licensing of your contribution.
