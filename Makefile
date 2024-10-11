VENV := venv
NODE_MODULES := node_modules
PYTHON_BIN := $(VENV)/bin/python
CDK_BIN := $(NODE_MODULES)/.bin/cdk

# Load environment variables from .env file
ifneq (,$(wildcard .env))
    include .env
    export $(shell sed 's/=.*//' .env)
endif

.PHONY deploy:
deploy:
	@echo "Deploying the stack..."
	@$(CDK_BIN) deploy --profile $(AWS_PROFILE)

.PHONY diff:
diff:
	@echo "Showing the differences..."
	@$(CDK_BIN) diff --profile $(AWS_PROFILE)

.PHONY: bootstrap
bootstrap: sso-login
	@echo "Bootstrapping the project..."
	@$(CDK_BIN) bootstrap --profile $(AWS_PROFILE)

.PHONY: sso-login
sso-login:
	@echo "Logging in to AWS SSO..."
	@aws sso login --profile $(AWS_PROFILE)

.PHONY: install
install:
	@echo "Installing dependencies..."
	@if [ ! -d $(VENV) ]; then \
		python3 -m venv $(VENV); \
	fi
	@$(PYTHON_BIN) -m pip install -r requirements.txt
	@npm install

.PHONY: clean
clean:
	@echo "Cleaning up..."
	@rm -rf $(VENV) $(NODE_MODULES)
