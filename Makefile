.PHONY: install init seed test ui ui-aws ui-locked deploy selfcheck clean

VENV := .venv
PY   := $(VENV)/bin/python
# 3.11: langchain-cockroachdb/psycopg wheels lag behind the newest CPython.
# Override if you know better:  make install PYBIN=python3.12
PYBIN ?= python3.11

install:                     ## create venv + install deps
	$(PYBIN) -m venv $(VENV)
	$(PY) -m pip install -U pip
	$(PY) -m pip install -r requirements.txt
	@echo "Now: cp .env.example .env  and fill COCKROACH_DB_URL"

init:                        ## create vector / chat / checkpoint tables in CockroachDB
	$(PY) scripts/init_db.py

seed:                        ## load freelancer rules + risk patterns into vector memory
	$(PY) scripts/seed_data.py

test:                        ## run the agent end-to-end on the sample risky contract
	$(PY) scripts/test_graph.py

ui:                          ## run the site locally at http://localhost:8000
	$(PY) scripts/serve.py 8000

ui-aws:                      ## same site, but every review runs on the deployed AWS Lambda
	LAMBDA_FUNCTION=freelance-guardian LAMBDA_REGION=ap-south-1 $(PY) scripts/serve.py 8000

# Set APP_PASSCODE to require sign-in. Unset (the default) leaves the workspace open,
# which is what you want when a judge is clicking through it.
ui-locked:                   ## same as ui-aws, behind a passcode: make ui-locked PASS=secret
	APP_PASSCODE=$(PASS) LAMBDA_FUNCTION=freelance-guardian LAMBDA_REGION=ap-south-1 \
		$(PY) scripts/serve.py 8000

deploy:                      ## build + deploy to AWS Lambda (idempotent)
	bash scripts/deploy_aws.sh

selfcheck:                   ## fast offline checks — no database needed
	$(PY) -m src.llm
	$(PY) -m src.s3

clean:
	rm -rf $(VENV) .local_s3 __pycache__ src/__pycache__
