import * as cdk from 'aws-cdk-lib';
import { Construct } from 'constructs';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as s3deploy from 'aws-cdk-lib/aws-s3-deployment';
import * as autoscaling from 'aws-cdk-lib/aws-autoscaling';
import * as path from 'path';

export class MyStack extends cdk.Stack {
    constructor(scope: Construct, id: string, props?: cdk.StackProps) {
        super(scope, id, props);

        // Create a VPC
        const vpc = new ec2.Vpc(this, 'MyVpc', {
            maxAzs: 2 // Default is all AZs in the region
        });

        // Create Security Group
        const securityGroup = new ec2.SecurityGroup(this, 'SecurityGroup', {
            vpc,
            allowAllOutbound: true,
        });

        securityGroup.addIngressRule(ec2.Peer.anyIpv4(), ec2.Port.tcp(22), 'Allow SSH');
        securityGroup.addIngressRule(ec2.Peer.anyIpv4(), ec2.Port.tcp(80), 'Allow HTTP');
        securityGroup.addIngressRule(ec2.Peer.anyIpv4(), ec2.Port.tcp(443), 'Allow HTTPS');

        // Create S3 Buckets
        const envBucket = new s3.Bucket(this, 'EnvBucket', {
            removalPolicy: cdk.RemovalPolicy.DESTROY,
        });

        const backupBucket = new s3.Bucket(this, 'BackupBucket', {
            removalPolicy: cdk.RemovalPolicy.RETAIN,
            lifecycleRules: [{
                // Rule to expire objects after 30 days
                expiration: cdk.Duration.days(30),
            }],
        });

        const dockerComposePath = path.join(__dirname, 'docker-compose.yml'); // Update the path to your docker-compose.yml file

        // Deploy docker-compose.yml file
        new s3deploy.BucketDeployment(this, 'DeployDockerCompose', {
            sources: [s3deploy.Source.asset(dockerComposePath)],
            destinationBucket: envBucket, // Assuming you want to keep both files in the same bucket
            retainOnDelete: false,
        });

        // Create an IAM Role with limited permissions
        const ec2Role = new iam.Role(this, 'Ec2Role', {
            assumedBy: new iam.ServicePrincipal('ec2.amazonaws.com'),
        });

        // Policy to allow uploading to backup bucket and reading from both buckets
        const policy = new iam.Policy(this, 'S3AccessPolicy', {
            statements: [
                new iam.PolicyStatement({
                    actions: [
                        "s3:PutObject",
                        "s3:PutObjectAcl",
                    ],
                    resources: [
                        backupBucket.bucketArn + '/*', // Allow uploads to the backup bucket
                    ],
                }),
                new iam.PolicyStatement({
                    actions: [
                        "s3:GetObject",
                        "s3:GetObjectAcl",
                    ],
                    resources: [
                        envBucket.bucketArn + '/*', // Allow reading the .env file from the env bucket
                        backupBucket.bucketArn + '/*', // Allow reading from the backup bucket
                    ],
                }),
            ],
        });

        // Attach the policy to the role
        ec2Role.attachInlinePolicy(policy);

        // Create an Auto Scaling Group
        const autoScalingGroup = new autoscaling.AutoScalingGroup(this, 'AutoScalingGroup', {
            vpc,
            instanceType: ec2.InstanceType.of(ec2.InstanceClass.T2, ec2.InstanceSize.MICRO),
            machineImage: ec2.MachineImage.latestAmazonLinux(),
            securityGroup,
            desiredCapacity: 1,
            minCapacity: 1,
            maxCapacity: 2,
            role: ec2Role, // Attach the IAM role
        });

        // User Data to install Docker, Docker Compose, and configure environment
        autoScalingGroup.addUserData(
            `#!/bin/bash`,
            `sudo yum update -y`,
            `sudo yum install -y docker`,
            `sudo systemctl start docker`,
            `sudo systemctl enable docker`,
            `sudo curl -L "https://github.com/docker/compose/releases/download/1.29.2/docker-compose-$(uname -s)-$(uname -m)" -o /usr/local/bin/docker-compose`,
            `sudo chmod +x /usr/local/bin/docker-compose`,
            `sudo usermod -aG docker ec2-user`,

            `export ACTUAL_BUDGET_DATA_PATH=/home/ec2-user/budget-data`, // Set the actual data path
            `export DOCKER_COMPOSE_FILE=/home/ec2-user/docker-compose.yml`, // Set the docker-compose file path
            `aws s3 cp s3://${envBucket.bucketName}/docker-compose.yml $DOCKER_COMPOSE_FILE`, // Download the docker-compose file

            `LATEST_BACKUP=$(aws s3 ls s3://${backupBucket.bucketName}/ | sort | tail -n 1 | awk '{print $4}')`, // Get the latest backup file name
            `if [ ! -z "$LATEST_BACKUP" ]; then`,
            `  echo "Latest backup file found: $LATEST_BACKUP, downloading..."`,
            `  aws s3 cp s3://${backupBucket.bucketName}/$LATEST_BACKUP ./`, // Download the latest backup file
            `  echo "Backup file downloaded."`,
            `  mkdir -p $ACTUAL_BUDGET_DATA_PATH`,
            `  tar -xzf $LATEST_BACKUP -C $ACTUAL_BUDGET_DATA_PATH/`, // Extract the downloaded tar file
            `else`,
            `  echo "No backup files found."`,
            `fi`,

            // Run docker-compose with the environment variable
            `sudo ACTUAL_BUDGET_DATA_PATH=$ACTUAL_BUDGET_DATA_PATH docker-compose up -d -f $DOCKER_COMPOSE_FILE`,

            // Create a script to tar the data and upload to S3
            `cat << 'EOF' > /home/ec2-user/backup_script.sh`,
            `#!/bin/bash`,
            `DATE=$(date +%Y%m%d_%H%M%S)`,
            `TAR_FILE="/home/ec2-user/actual_budget_data_$DATE.tar.gz"`,
            `tar -czf $TAR_FILE -C $ACTUAL_BUDGET_DATA_PATH .`,
            `aws s3 cp $TAR_FILE s3://${backupBucket.bucketName}/`,
            `rm $TAR_FILE`, // Remove the tar file after uploading
            `EOF`,
            `chmod +x /home/ec2-user/backup_script.sh`,

            // Schedule the cron job to run every 3 days at 4 AM
            `(crontab -l 2>/dev/null; echo "0 4 */3 * * /home/ec2-user/backup_script.sh") | crontab -`,
        );

        // Outputs
        new cdk.CfnOutput(this, 'EC2PublicIP', { value: autoScalingGroup.autoScalingGroupName });
        new cdk.CfnOutput(this, 'BackupBucketName', { value: backupBucket.bucketName });
        new cdk.CfnOutput(this, 'EnvBucketURL', { value: envBucket.bucketRegionalDomainName }); // Output the Env Bucket URL
    }
}
