import os
from dotenv import load_dotenv

from aws_cdk import App, Environment
import aws_cdk as cdk

from actual_budget_cdk.actual_budget_cdk_stack import ActualBudgetCdkStack

# Load environment variables from the .env file
load_dotenv()

# Retrieve environment variables
account = os.getenv("ACCOUNT_ID")
region = os.getenv("REGION")

app = cdk.App()
ActualBudgetCdkStack(app, "ActualBudgetCdkStack",
                     env=Environment(account=account, region=region))

app.synth()
