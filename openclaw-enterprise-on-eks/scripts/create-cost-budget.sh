#!/usr/bin/env bash
set -euo pipefail
# shellcheck source=common.sh
source "$(dirname "$0")/common.sh"
load_config

for command in aws jq; do require_command "$command"; done

monthly_budget_usd="${MONTHLY_BUDGET_USD:-}"
budget_email="${BUDGET_EMAIL:-}"
budget_name="${BUDGET_NAME:-$CLUSTER_NAME-monthly-cost}"

[[ "$monthly_budget_usd" =~ ^([1-9][0-9]*([.][0-9]{1,2})?|0[.][0-9]?[1-9])$ ]] ||
  die "MONTHLY_BUDGET_USD must be a positive USD amount"
[[ "$budget_email" =~ ^[^[:space:]@]+@[^[:space:]@]+[.][^[:space:]@]+$ ]] ||
  die "BUDGET_EMAIL must be a valid email address"

account_id="$(aws sts get-caller-identity --query Account --output text)"
[[ "$account_id" =~ ^[0-9]{12}$ ]] || die "could not resolve the AWS account"

budget_error="$GENERATED_DIR/budget-error.log"
if aws budgets describe-budget \
  --account-id "$account_id" \
  --budget-name "$budget_name" >/dev/null 2>"$budget_error"; then
  die "budget already exists: $budget_name"
elif ! grep -q NotFoundException "$budget_error"; then
  cat "$budget_error" >&2
  die "could not inspect budget $budget_name"
fi
rm -f -- "$budget_error"

budget_file="$GENERATED_DIR/budget.json"
notifications_file="$GENERATED_DIR/budget-notifications.json"
jq -n \
  --arg name "$budget_name" \
  --arg amount "$monthly_budget_usd" \
  '{
    BudgetName:$name,
    BudgetLimit:{Amount:$amount,Unit:"USD"},
    TimeUnit:"MONTHLY",
    BudgetType:"COST"
  }' > "$budget_file"
jq -n \
  --arg email "$budget_email" \
  '[
    {
      Notification:{
        NotificationType:"FORECASTED",
        ComparisonOperator:"GREATER_THAN",
        Threshold:80,
        ThresholdType:"PERCENTAGE"
      },
      Subscribers:[{SubscriptionType:"EMAIL",Address:$email}]
    },
    {
      Notification:{
        NotificationType:"ACTUAL",
        ComparisonOperator:"GREATER_THAN",
        Threshold:100,
        ThresholdType:"PERCENTAGE"
      },
      Subscribers:[{SubscriptionType:"EMAIL",Address:$email}]
    }
  ]' > "$notifications_file"
chmod 600 "$budget_file" "$notifications_file"

aws budgets create-budget \
  --account-id "$account_id" \
  --budget "file://$budget_file" \
  --notifications-with-subscribers "file://$notifications_file"

printf 'Created account-wide monthly budget %s for %s USD.\n' \
  "$budget_name" "$monthly_budget_usd"
