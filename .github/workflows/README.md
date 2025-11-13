# GitHub Actions Workflows

## trigger-fieldworks-patch.yml

This workflow automatically triggers a patch build in the [sillsdev/FieldWorks](https://github.com/sillsdev/FieldWorks) repository whenever changes are merged into the `develop` branch of this repository.

### Required Setup

For this workflow to function properly, you need to configure a secret in this repository:

1. Create a GitHub Personal Access Token (PAT) with the following permissions:
   - `repo` scope (or at minimum `public_repo` if the FieldWorks repo is public)
   - `workflow` scope (required to trigger workflows in other repositories)

2. Add the token as a repository secret:
   - Go to this repository's Settings → Secrets and variables → Actions
   - Click "New repository secret"
   - Name: `FIELDWORKS_WORKFLOW_TOKEN`
   - Value: Your PAT created in step 1

3. Update the workflow file to use this secret instead of `GITHUB_TOKEN`:
   ```yaml
   github-token: ${{ secrets.FIELDWORKS_WORKFLOW_TOKEN }}
   ```

### How It Works

When a push to the `develop` branch occurs:
1. The workflow is triggered automatically
2. It calls the `patch-installer-cd.yml` workflow in the FieldWorks repository
3. It passes the commit SHA from FwHelps as the `helps_ref` parameter
4. The FieldWorks workflow will check out the specific version of FwHelps that was just merged
5. A new patch installer will be built with the updated help files

The workflow targets the `release/9.3` branch by default. To target a different FieldWorks release branch, update the `FIELDWORKS_BRANCH` environment variable in the workflow file.

### Changes Required to FieldWorks Workflow

**No changes are required** to the FieldWorks `patch-installer-cd.yml` workflow. The workflow already has `workflow_dispatch` enabled with all the necessary inputs, including `helps_ref` which defaults to `develop`.

### Testing

To test this workflow without waiting for a merge to develop:
1. Manually trigger the FieldWorks patch build workflow from the Actions tab in the FieldWorks repository
2. Set the `helps_ref` input to a specific branch or commit SHA from this repository
3. Verify the build completes successfully

### Troubleshooting

If the workflow fails with a 404 or authentication error:
- Verify the `FIELDWORKS_WORKFLOW_TOKEN` secret is properly configured
- Ensure the token has the `workflow` scope
- Check that the token hasn't expired
- Verify the workflow file name and branch are correct in the trigger action
