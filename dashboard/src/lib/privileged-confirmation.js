const label = value => String(value || '').replaceAll('_', ' ')

export function memberRoleChangeConfirmation(member, nextRole) {
  return {
    title: `Change role for member ${member.id}?`,
    description: `This changes member ${member.id} from ${label(member.role)} to ${label(nextRole)} and updates their permissions immediately.`,
    confirmLabel: 'Change role',
    tone: 'warning',
  }
}

export function memberStatusConfirmation(member, nextStatus) {
  if (nextStatus === 'active') {
    return {
      title: `Enable member ${member.id}?`,
      description: `This restores company access for member ${member.id} with the ${label(member.role)} role.`,
      confirmLabel: 'Enable member',
      tone: 'warning',
    }
  }

  if (nextStatus === 'disabled') {
    return {
      title: `Disable member ${member.id}?`,
      description: `Member ${member.id} will immediately lose company access until an administrator enables them again.`,
      confirmLabel: 'Disable member',
      tone: 'danger',
    }
  }

  if (nextStatus === 'removed') {
    return {
      title: `Remove member ${member.id}?`,
      description: `Member ${member.id} will immediately lose access and will be removed from this company. This cannot be undone from the dashboard.`,
      confirmLabel: 'Remove member',
      tone: 'danger',
    }
  }

  throw new Error(`Unsupported member status: ${nextStatus}`)
}

export function serviceAccountRevocationConfirmation(account) {
  return {
    title: `Revoke service account ${account.name}?`,
    description: `Service account ${account.name} (${account.id}) will immediately lose access. Deployed systems using its key will stop working.`,
    confirmLabel: 'Revoke service account',
    tone: 'danger',
  }
}
