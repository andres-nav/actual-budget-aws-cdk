#!/usr/bin/env python3

from aws_cdk import (
    Stack,
    Duration,
    RemovalPolicy,
    CfnOutput,
    Fn,
    aws_autoscaling as autoscaling,
    aws_ec2 as ec2,
    aws_iam as iam,
    aws_s3 as s3,
    aws_s3_deployment as s3_deployment,
)
from constructs import Construct
import os
import shutil
import tempfile
import zipfile
import requests

from dotenv import load_dotenv

# Load environment variables from the .env file
load_dotenv()

domain_name = os.getenv("DOMAIN_NAME")

def get_ec2_instance_connect_ip(region: str) -> str:
    # Get the latest AWS IP ranges
    url = "https://ip-ranges.amazonaws.com/ip-ranges.json"
    response = requests.get(url)
    response.raise_for_status()
    ip_ranges = response.json()["prefixes"]

    # Find the EC2 Instance Connect IP range for the specified region
    for entry in ip_ranges:
        if entry["service"] == "EC2_INSTANCE_CONNECT" and entry["region"] == region:
            return entry["ip_prefix"]

    raise ValueError(f"No EC2 Instance Connect IP range found for region: {region}")

class ActualBudgetCdkStack(Stack):
    def __init__(self, scope: Construct, id: str, **kwargs):
        super().__init__(scope, id, **kwargs)

        # Use the default VPC
        vpc = ec2.Vpc.from_lookup(self, "DefaultVPC", is_default=True)

        # Create Security Group
        security_group = ec2.SecurityGroup(self, 'SecurityGroup', vpc=vpc, allow_all_outbound=True)
        security_group.add_ingress_rule(ec2.Peer.any_ipv4(), ec2.Port.tcp(80), 'Allow HTTP')
        security_group.add_ingress_rule(ec2.Peer.any_ipv4(), ec2.Port.tcp(443), 'Allow HTTPS')

        # Allow SSH from EC2 Instance Connect
        ec2_instance_connect_ip = get_ec2_instance_connect_ip(self.region)
        security_group.add_ingress_rule(ec2.Peer.ipv4(ec2_instance_connect_ip), ec2.Port.tcp(22), 'Allow SSH from EC2 Instance Connect')

        # Create S3 Buckets
        env_bucket = s3.Bucket(self, 'EnvBucket', removal_policy=RemovalPolicy.DESTROY)
        backup_bucket = s3.Bucket(
            self, 'BackupBucket',
            removal_policy=RemovalPolicy.RETAIN,
            lifecycle_rules=[{
                'expiration': Duration.days(30),
            }]
        )

        docker_compose_path = os.path.join(os.path.dirname(__file__), 'docker-compose.yml')  # Path to docker-compose.yml

        # Deploy docker-compose.yml file to S3
        with tempfile.TemporaryDirectory() as temp_dir:
            shutil.copy(docker_compose_path, temp_dir)
            zip_path = os.path.join(temp_dir, 'docker-compose.zip')
            with zipfile.ZipFile(zip_path, 'w') as zipf:
                zipf.write(os.path.join(temp_dir, 'docker-compose.yml'), arcname='docker-compose.yml')

            s3_deployment.BucketDeployment(
                self, 'DeployDockerCompose',
                sources=[s3_deployment.Source.asset(zip_path)],
                destination_bucket=env_bucket,
                retain_on_delete=False,
            )

        # Create an IAM Role with limited permissions
        ec2_role = iam.Role(self, 'Ec2Role', assumed_by=iam.ServicePrincipal('ec2.amazonaws.com'))

        # Policy to allow access to the S3 buckets
        ec2_role.attach_inline_policy(
            iam.Policy(self, 'S3AccessPolicy', statements=[
                iam.PolicyStatement(actions=["s3:PutObject", "s3:PutObjectAcl", "s3:ListBucket", "s3:ListObjectsV2", "s3:GetObject", "s3:GetObjectAcl"], resources=[f"{backup_bucket.bucket_arn}/*"]),
                iam.PolicyStatement(actions=["s3:GetObject", "s3:GetObjectAcl"], resources=[
                    f"{env_bucket.bucket_arn}/*",
                    f"{backup_bucket.bucket_arn}/*"
                ]),
            ])
        )

        dir = "/home/ec2-user"
        actual_budget_data_path = f"{dir}/data"
        cloud_init_file = os.path.join(os.path.dirname(__file__), 'cloud-init.sh')

        # Read the cloud-init.sh file and replace placeholders
        with open(cloud_init_file, 'r') as file:
            cloud_init_script = file.read()

        # Substitute the placeholders with actual values
        cloud_init_script = cloud_init_script.format(
            dir=dir,
            actual_budget_data_path=actual_budget_data_path,
            domain_name=domain_name,
            env_bucket_name=env_bucket.bucket_name,
            backup_bucket_name=backup_bucket.bucket_name,
        )

        # Define Launch Template for EC2 instances
        launch_template = ec2.LaunchTemplate(
            self,
            "LaunchTemplate",
            instance_type=ec2.InstanceType("t2.micro"),
            machine_image=ec2.MachineImage.latest_amazon_linux2(),
            security_group=security_group,
            role=ec2_role,
            block_devices=[
                ec2.BlockDevice(
                    device_name="/dev/xvda",
                    volume=ec2.BlockDeviceVolume.ebs(8, encrypted=True),
                )
            ],
            user_data=ec2.UserData.custom(cloud_init_script)
        )

        # Create Auto Scaling Group using Launch Template
        auto_scaling_group = autoscaling.AutoScalingGroup(
            self, 'AutoScalingGroup',
            vpc=vpc,
            launch_template=launch_template,
            min_capacity=1,
            max_capacity=2,
        )

        # Outputs
        CfnOutput(self, 'BackupBucketName', value=backup_bucket.bucket_name)
        CfnOutput(self, 'EnvBucketURL', value=env_bucket.bucket_regional_domain_name)
