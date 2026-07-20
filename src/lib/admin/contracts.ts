export const COMPANY_ADMIN_METHODS = ['GET', 'POST', 'PATCH', 'DELETE'] as const;

export type CompanyRole =
  | 'company_owner'
  | 'company_admin'
  | 'member'
  | 'billing_admin';

export type CompanyMemberStatus = 'active' | 'disabled' | 'removed';

export interface CursorPage<T> {
  items: T[];
  next_cursor: string;
  has_more: boolean;
  limit: number;
}

export interface CompanyCapabilities {
  company_id: string;
  role: CompanyRole;
  permissions: string[];
  companies: CompanyChoice[];
}

export interface CompanyChoice {
  company_id: string;
  company_name: string;
  role: CompanyRole;
}

export interface CompanyMember {
  id: string;
  role: CompanyRole;
  status: CompanyMemberStatus;
  created_at: string;
}

export interface CompanyServiceAccount {
  id: string;
  name: string;
  environment: string;
  scopes: string[];
  status: 'active' | 'revoked';
  expires_at: string | null;
  revoked_at: string | null;
  created_at: string;
}

export interface CompanyAuditEvent {
  id: number;
  request_id: string;
  actor_id: string;
  actor_role: string;
  action: string;
  target_type: string;
  target_id: string;
  outcome: 'committed' | 'denied';
  occurred_at: string;
}
