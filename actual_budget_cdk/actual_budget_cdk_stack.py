#!/usr/bin/env python3

from aws_cdk import (
    Stack,
    Duration,
    RemovalPolicy,
    CfnOutput,
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


class ActualBudgetCdkStack(Stack):
    def __init__(self, scope: Construct, id: str, **kwargs):
        super().__init__(scope, id, **kwargs)

        # Use the default VPC
        vpc = ec2.Vpc.from_lookup(self, "DefaultVPC", is_default=True)

        # Create Security Group
        security_group = ec2.SecurityGroup(self, 'SecurityGroup', vpc=vpc, allow_all_outbound=True)
        security_group.add_ingress_rule(ec2.Peer.any_ipv4(), ec2.Port.tcp(22), 'Allow SSH')
        security_group.add_ingress_rule(ec2.Peer.any_ipv4(), ec2.Port.tcp(80), 'Allow HTTP')
        security_group.add_ingress_rule(ec2.Peer.any_ipv4(), ec2.Port.tcp(443), 'Allow HTTPS')

        # Create S3 Buckets
        env_bucket = s3.Bucket(self, 'EnvBucket', removal_policy=RemovalPolicy.DESTROY)
        backup_bucket = s3.Bucket(
            self, 'BackupBucket',
            removal_policy=RemovalPolicy.RETAIN,
            lifecycle_rules=[{
                'expiration': Duration.days(30),
                'noncurrent_version_expiration': Duration.days(30),
                'abort_incomplete_multipart_upload_after': Duration.days(7),
                'max_count': 5  # Retain the last 5 files
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
                iam.PolicyStatement(actions=["s3:PutObject", "s3:PutObjectAcl"], resources=[f"{backup_bucket.bucket_arn}/*"]),
                iam.PolicyStatement(actions=["s3:GetObject", "s3:GetObjectAcl"], resources=[
                    f"{env_bucket.bucket_arn}/*",
                    f"{backup_bucket.bucket_arn}/*"
                ]),
            ])
        )

        # Define Launch Template for EC2 instances
        launch_template = ec2.CfnLaunchTemplate(
            self,
            "LaunchTemplate",
            launch_template_data={
                "imageId": ec2.MachineImage.latest_amazon_linux2().get_image(self).image_id,
                "instanceType": "t2.micro",
                "securityGroupIds": [security_group.security_group_id],
                "userData": ec2.Fn.base64(f"""#!/bin/bash
                sudo yum update -y
                sudo yum install -y docker unzip

                DOCKER_COMPOSE_ZIP="/home/ec2-user/docker-compose.zip"
                DOCKER_COMPOSE_FILE="/home/ec2-user/docker-compose.yml"

                aws s3 cp s3://{env_bucket.bucket_name}/docker-compose.zip $DOCKER_COMPOSE_ZIP
                unzip $DOCKER_COMPOSE_ZIP -d /home/ec2-user/

                sudo systemctl start docker
                sudo systemctl enable docker
                sudo curl -L "https://github.com/docker/compose/releases/download/1.29.2/docker-compose-$(uname -s)-$(uname -m)" -o /usr/local/bin/docker-compose
                sudo chmod +x /usr/local/bin/docker-compose
                sudo usermod -aG docker ec2-user

                export ACTUAL_BUDGET_DATA_PATH=/home/ec2-user/budget-data

                LATEST_BACKUP=$(aws s3 ls s3://{backup_bucket.bucket_name}/ | sort | tail -n 1 | awk '{{print $4}}')
                if [ ! -z "$LATEST_BACKUP" ]; then
                    echo "Latest backup file found: $LATEST_BACKUP, downloading..."
                    aws s3 cp s3://{backup_bucket.bucket_name}/$LATEST_BACKUP ./
                    echo "Backup file downloaded."
                    mkdir -p $ACTUAL_BUDGET_DATA_PATH
                    tar -xzf $LATEST_BACKUP -C $ACTUAL_BUDGET_DATA_PATH/
                else
                    echo "No backup files found."
                fi

                sudo ACTUAL_BUDGET_DATA_PATH=$ACTUAL_BUDGET_DATA_PATH docker-compose up -d -f $DOCKER_COMPOSE_FILE

                cat << 'EOF' > /home/ec2-user/backup_script.sh
                #!/bin/bash
                DATE=$(date +%Y%m%d_%H%M%S)
                TAR_FILE="/home/ec2-user/actual_budget_data_$DATE.tar.gz"
                tar -czf $TAR_FILE -C $ACTUAL_BUDGET_DATA_PATH .
                aws s3 cp $TAR_FILE s3://{backup_bucket.bucket_name}/
                rm $TAR_FILE
                EOF
                chmod +x /home/ec2-user/backup_script.sh

                (crontab -l 2>/dev/null; echo "0 4 */3 * * /home/ec2-user/backup_script.sh") | crontab -
                """)
            }
        )

        # Create Auto Scaling Group using Launch Template
        auto_scaling_group = autoscaling.AutoScalingGroup(
            self, 'AutoScalingGroup',
            vpc=vpc,
            launch_template=autoscaling.LaunchTemplateSpec(launch_template=launch_template),
            min_capacity=1,
            max_capacity=2
        )

        # Outputs
        CfnOutput(self, 'BackupBucketName', value=backup_bucket.bucket_name)
        CfnOutput(self, 'EnvBucketURL', value=env_bucket.bucket_regional_domain_name)
