#!/bin/bash
# NetPulse — environment setup script
# Run once after cloning: bash setup.sh

set -e

echo "================================================"
echo "  NetPulse — environment setup"
echo "================================================"

# 1. Check Python
if ! command -v python3 &> /dev/null; then
    echo "ERROR: python3 is required. Install from https://python.org"
    exit 1
fi
echo "✓ Python: $(python3 --version)"

# 2. Check Node (required for CDK CLI)
if ! command -v node &> /dev/null; then
    echo "ERROR: Node.js is required for CDK. Install from https://nodejs.org"
    exit 1
fi
echo "✓ Node: $(node --version)"

# 3. Check AWS CLI
if ! command -v aws &> /dev/null; then
    echo "WARNING: AWS CLI not found. Install from https://aws.amazon.com/cli/"
    echo "         You'll need it to bootstrap CDK and deploy."
fi

# 4. Install CDK CLI globally (if not present)
if ! command -v cdk &> /dev/null; then
    echo "Installing AWS CDK CLI..."
    npm install -g aws-cdk
fi
echo "✓ CDK: $(cdk --version)"

# 5. Create and activate virtual environment
echo ""
echo "Creating Python virtual environment..."
python3 -m venv .venv
source .venv/bin/activate

# 6. Install Python dependencies
echo "Installing Python dependencies..."
pip install --upgrade pip --quiet
pip install -r requirements.txt --quiet

echo ""
echo "================================================"
echo "  Setup complete!"
echo "================================================"
echo ""
echo "Next steps:"
echo ""
echo "  1. Activate the venv (do this every session):"
echo "     source .venv/bin/activate"
echo ""
echo "  2. Configure your AWS credentials:"
echo "     aws configure"
echo ""
echo "  3. Bootstrap CDK (one-time per AWS account/region):"
echo "     cdk bootstrap aws://YOUR_ACCOUNT_ID/us-east-1"
echo ""
echo "  4. Deploy NetPulse:"
echo "     cdk deploy"
echo ""
echo "  5. Run tests:"
echo "     pytest tests/ -v"
echo ""
