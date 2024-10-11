#!/bin/bash
sudo yum update -y
sudo yum install -y docker unzip

DIR="{dir}"
DOCKER_COMPOSE_FILENAME="docker-compose.yml"
DOCKER_COMPOSE_FILE="$DIR/$DOCKER_COMPOSE_FILENAME"
ACTUAL_BUDGET_DATA_PATH="{actual_budget_data_path}"
ENV_FILE="$DIR/.env"

aws s3 cp s3://{env_bucket_name}/$DOCKER_COMPOSE_FILENAME $DOCKER_COMPOSE_FILE
sudo chown ec2-user:ec2-user $DOCKER_COMPOSE_FILE

sudo systemctl start docker
sudo systemctl enable docker

sudo curl -L "https://github.com/docker/compose/releases/download/1.29.2/docker-compose-$(uname -s)-$(uname -m)" -o /usr/local/bin/docker-compose
sudo chmod +x /usr/local/bin/docker-compose
sudo usermod -aG docker ec2-user

cat <<EOF > $DIR/.env
ACTUAL_BUDGET_DATA_PATH=$ACTUAL_BUDGET_DATA_PATH
DOMAIN_NAME={domain_name}
EOF

sudo chown ec2-user:ec2-user $ENV_FILE

LATEST_BACKUP=$(aws s3 ls s3://{backup_bucket_name}/ | sort | tail -n 1 | awk '{{print $4}}')
if [ ! -z "$LATEST_BACKUP" ]; then
echo "Latest backup file found: $LATEST_BACKUP, downloading..."
aws s3 cp s3://{backup_bucket_name}/$LATEST_BACKUP ./
echo "Backup file downloaded."
mkdir -p $ACTUAL_BUDGET_DATA_PATH
tar -xzf $LATEST_BACKUP -C $ACTUAL_BUDGET_DATA_PATH/
else
echo "No backup files found."
fi

sudo /usr/local/bin/docker-compose --file $DOCKER_COMPOSE_FILE --env-file $ENV_FILE up -d

BACKUP_SCRIPT_PATH="$DIR/backup_script.sh"

cat <<EOF > $BACKUP_SCRIPT_PATH
#!/bin/bash
DATE=\$(date +%Y%m%d_%H%M%S)
ACTUAL_BUDGET_DATA_PATH="{actual_budget_data_path}"

TAR_FILE="/home/ec2-user/actual_budget_data_\$DATE.tar.gz"
tar -czf \$TAR_FILE -C "\$ACTUAL_BUDGET_DATA_PATH" .
aws s3 cp \$TAR_FILE s3://{backup_bucket_name}/
rm \$TAR_FILE
EOF

sudo chown ec2-user:ec2-user $BACKUP_SCRIPT_PATH
chmod +x $BACKUP_SCRIPT_PATH

echo "0 4 */3 * * $BACKUP_SCRIPT_PATH" >> /etc/crontab
