#!/usr/bin/env python3

from aws_cdk import (
    Stack,
    Duration,
    RemovalPolicy,
    CfnOutput,
)
from aws_cdk.aws_ec2 import (
    Vpc,
    Peer,
    Port,
    SecurityGroup,
    InstanceType,
    InstanceClass,
    InstanceSize,
    MachineImage,
)
from aws_cdk.aws_s3 import Bucket
from aws_cdk.aws_s3_deployment import BucketDeployment, Source
from aws_cdk.aws_iam import Role, ServicePrincipal, Policy, PolicyStatement
from aws_cdk.aws_autoscaling import AutoScalingGroup
from constructs import Construct
import os
import shutil
import tempfile
import zipfile

class ActualBudgetCdkStack(Stack):
    def __init__(self, scope: Construct, id: str, **kwargs):
        super().__init__(scope, id, **kwargs)

        # Create a VPC
        vpc = Vpc(self, 'MyVpc', max_azs=2)  # Default is all AZs in the region

        # Create Security Group
        security_group = SecurityGroup(self, 'SecurityGroup', vpc=vpc, allow_all_outbound=True)
        security_group.add_ingress_rule(Peer.any_ipv4(), Port.tcp(22), 'Allow SSH')
        security_group.add_ingress_rule(Peer.any_ipv4(), Port.tcp(80), 'Allow HTTP')
        security_group.add_ingress_rule(Peer.any_ipv4(), Port.tcp(443), 'Allow HTTPS')

        # Create S3 Buckets
        env_bucket = Bucket(self, 'EnvBucket', removal_policy=RemovalPolicy.DESTROY)
        backup_bucket = Bucket(
            self, 'BackupBucket',
            removal_policy=RemovalPolicy.RETAIN,
            lifecycle_rules=[{
                'expiration': Duration.days(30)  # Rule to expire objects after 30 days
            }]
        )

        docker_compose_path = os.path.join(os.path.dirname(__file__), 'docker-compose.yml')  # Path to your docker-compose.yml file

        # Deploy docker-compose.yml file
        # Create a temporary directory to zip the docker-compose.yml
        with tempfile.TemporaryDirectory() as temp_dir:
            # Copy the docker-compose.yml file to the temp directory
            shutil.copy(docker_compose_path, temp_dir)

            # Create a zip file from the directory
            zip_path = os.path.join(temp_dir, 'docker-compose.zip')
            with zipfile.ZipFile(zip_path, 'w') as zipf:
                zipf.write(os.path.join(temp_dir, 'docker-compose.yml'), arcname='docker-compose.yml')

            # Deploy the zip file to the S3 bucket
            BucketDeployment(
                self, 'DeployDockerCompose',
                sources=[Source.asset(zip_path)],
                destination_bucket=env_bucket,
                retain_on_delete=False,
            )

        # Create an IAM Role with limited permissions
        ec2_role = Role(
            self, 'Ec2Role',
            assumed_by=ServicePrincipal('ec2.amazonaws.com'),
        )

        # Policy to allow uploading to backup bucket and reading from both buckets
        policy = Policy(self, 'S3AccessPolicy')

        # Add statements to the policy
        policy.add_statements(
            PolicyStatement(
                actions=["s3:PutObject", "s3:PutObjectAcl"],
                resources=[f"{backup_bucket.bucket_arn}/*"],  # Allow uploads to the backup bucket
            ),
            PolicyStatement(
                actions=["s3:GetObject", "s3:GetObjectAcl"],
                resources=[
                    f"{env_bucket.bucket_arn}/*",  # Allow reading the .env file from the env bucket
                    f"{backup_bucket.bucket_arn}/*",  # Allow reading from the backup bucket
                ],
            ),
        )

        # Attach the policy to the role
        ec2_role.attach_inline_policy(policy)

        # Create an Auto Scaling Group
        auto_scaling_group = AutoScalingGroup(
            self, 'AutoScalingGroup',
            vpc=vpc,
            instance_type=InstanceType.of(InstanceClass.T2, InstanceSize.MICRO),
            machine_image=MachineImage.latest_amazon_linux2(),
            security_group=security_group,
            desired_capacity=1,
            min_capacity=1,
            max_capacity=2,
            role=ec2_role,  # Attach the IAM role
        )

        # User Data to install Docker, Docker Compose, and configure environment
        user_data_script = f"""#!/bin/bash
        sudo yum update -y
        sudo yum install -y docker unzip  # Install unzip

        # Define variables for paths
        DOCKER_COMPOSE_ZIP="/home/ec2-user/docker-compose.zip"
        DOCKER_COMPOSE_FILE="/home/ec2-user/docker-compose.yml"

        # Download the zip file containing the docker-compose.yml
        aws s3 cp s3://{env_bucket.bucket_name}/docker-compose.zip $DOCKER_COMPOSE_ZIP

        # Extract the zip file
        unzip $DOCKER_COMPOSE_ZIP -d /home/ec2-user/  # Extract to /home/ec2-user/

        sudo systemctl start docker
        sudo systemctl enable docker
        sudo curl -L "https://github.com/docker/compose/releases/download/1.29.2/docker-compose-$(uname -s)-$(uname -m)" -o /usr/local/bin/docker-compose
        sudo chmod +x /usr/local/bin/docker-compose
        sudo usermod -aG docker ec2-user

        export ACTUAL_BUDGET_DATA_PATH=/home/ec2-user/budget-data

        LATEST_BACKUP=$(aws s3 ls s3://{backup_bucket.bucket_name}/ | sort | tail -n 1 | awk '{{print $4}}')  # Get the latest backup file name
        if [ ! -z "$LATEST_BACKUP" ]; then
            echo "Latest backup file found: $LATEST_BACKUP, downloading..."
            aws s3 cp s3://{backup_bucket.bucket_name}/$LATEST_BACKUP ./  # Download the latest backup file
            echo "Backup file downloaded."
            mkdir -p $ACTUAL_BUDGET_DATA_PATH
            tar -xzf $LATEST_BACKUP -C $ACTUAL_BUDGET_DATA_PATH/  # Extract the downloaded tar file
        else
            echo "No backup files found."
        fi

        # Run docker-compose with the environment variable
        sudo ACTUAL_BUDGET_DATA_PATH=$ACTUAL_BUDGET_DATA_PATH docker-compose up -d -f $DOCKER_COMPOSE_FILE

        # Create a script to tar the data and upload to S3
        cat << 'EOF' > /home/ec2-user/backup_script.sh
        #!/bin/bash
        DATE=$(date +%Y%m%d_%H%M%S)
        TAR_FILE="/home/ec2-user/actual_budget_data_$DATE.tar.gz"
        tar -czf $TAR_FILE -C $ACTUAL_BUDGET_DATA_PATH .
        aws s3 cp $TAR_FILE s3://{backup_bucket.bucket_name}/
        rm $TAR_FILE  # Remove the tar file after uploading
        EOF
        chmod +x /home/ec2-user/backup_script.sh

        # Schedule the cron job to run every 3 days at 4 AM
        (crontab -l 2>/dev/null; echo "0 4 */3 * * /home/ec2-user/backup_script.sh") | crontab -
        """

        auto_scaling_group.add_user_data(user_data_script)

        # Outputs
        CfnOutput(self, 'EC2PublicIP', value=auto_scaling_group.auto_scaling_group_name)  # Outputs the Auto Scaling Group name
        CfnOutput(self, 'BackupBucketName', value=backup_bucket.bucket_name)
        CfnOutput(self, 'EnvBucketURL', value=env_bucket.bucket_regional_domain_name)  # Output the Env Bucket URL
