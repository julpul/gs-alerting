| Symptom                         | Likely cause        | Do this                                   | Approval |
|---------------------------------|---------------------|-------------------------------------------|----------|
| Errors in one region only       | Regional outage     | Fail traffic over to the other region     | Team lead|
| Errors in all regions           | Bad deployment      | Roll back the last deployment             | Required |
| Errors only on /transfer        | Downstream bank API | Open a ticket with the integrations team  | None     |
