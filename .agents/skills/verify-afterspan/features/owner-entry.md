# Owner entry

## Sub-features

Local sign-in, empty workspace, populated project selector, sign-out and expired access.

## How to get to it (user POV)

Open `/app`. Enter the synthetic owner's email and follow its private local sign-in link. Select the synthetic project.

## Driving it with the browser

Use the Email address field and Email me a sign-in link button. Read only this run's local `.eml` file. Follow its link without exposing the token in a screenshot or output. Confirm the account role and project name, then open Goal loop. Sign out and confirm project data is hidden. Failure is the wrong account/project, stale data after sign-out or an unhandled error.

## Gotchas

Local email does not prove SMTP or inbox delivery. No customer signup or invitation is created. Existing-owner password mode has separate workspace limits and is not an email account.
