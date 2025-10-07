# Github Workflow Backup

## ci.yml

```yml
name: Module CI
on:
  pull_request:
    types: [opened, synchronize, reopened]
  push:
    branches:
      - main
      - 'release/**'
      - develop
      - 'feature/**'
  workflow_dispatch:
    inputs:
      comment:
        description: Just a simple comment to know the purpose of the manual build
        required: false

jobs:
  call:
    name: Default CI Flow
    uses: openimis/openimis-be_py/.github/workflows/ci_module.yml@develop
    secrets:
      SONAR_TOKEN: ${{ secrets.SONAR_TOKEN }}
    with:
      SONAR_PROJECT_KEY: openimis_openimis-be-api_etl_py
      SONAR_ORGANIZATION: openimis-1
      SONAR_PROJECT_NAME: openimis-be-api_etl_py
      SONAR_PROJECT_VERSION: 1.0
      SONAR_SOURCES: workflow
      SONAR_EXCLUSIONS: "**/migrations/**,**/static/**,**/media/**,**/tests/**"
```

## core-mis-test-server-deploy.yml
```yml
name: CoreMIS Server Deployment
on:
  push:
    branches:
      - develop

jobs:
  rebuild-test-server:
    runs-on: ubuntu-latest
    steps:
      - name: Check out code
        uses: actions/checkout@v2

      - name: Set up SSH
        run: |
          mkdir -p ~/.ssh
          echo "${{ secrets.CORE_MIS_DEPLOYMENT_SSH_KEY }}" > ~/.ssh/id_rsa
          chmod 600 ~/.ssh/id_rsa
          ssh-keyscan -H ${{ secrets.CORE_MIS_DEPLOYMENT_HOST }} >> ~/.ssh/known_hosts
        env:
          CORE_MIS_DEPLOYMENT_SSH_KEY: ${{ secrets.CORE_MIS_DEPLOYMENT_SSH_KEY }}
          CORE_MIS_DEPLOYMENT_USER: ${{ secrets.CORE_MIS_DEPLOYMENT_USER }}
          CORE_MIS_DEPLOYMENT_HOST: ${{ secrets.CORE_MIS_DEPLOYMENT_HOST }}

      - name: Run Docker Compose
        run: |
          ssh -o StrictHostKeyChecking=no -T ${{ secrets.CORE_MIS_DEPLOYMENT_USER }}@${{ secrets.CORE_MIS_DEPLOYMENT_HOST }} -p 1022
          ssh ${{ secrets.CORE_MIS_DEPLOYMENT_USER }}@${{ secrets.CORE_MIS_DEPLOYMENT_HOST }} -p 1022 << EOF
            cd coreMIS/
            docker-compose build backend gateway && docker-compose up -d
          EOF
          
```
