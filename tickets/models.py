from django.db import models
from django.contrib.auth.models import User

# 1. CORPORATE DEPARTMENTS
DEPARTMENT_CHOICES = [
    ('ADMIN', 'Admin'),
    ('ACTUARIAL', 'Actuarial'),
    ('OPERATIONS', 'Operations'),
    ('FINANCE', 'Finance & Accounting'),
    ('HR', 'HR'),
    ('IT', 'IT'),
    ('PROVIDERS', 'Providers'),
    ('MARKETING', 'Marketing'),
    ('CLAIMS', 'Claims'),
    ('UNDERWRITING', 'Underwriting'),
]

IT_TIER_CHOICES = [
    ('TIER_1', 'Tier 1 — First Response'),
    ('TIER_2', 'Tier 2 — Advanced Support'),
]

# 2. EMPLOYEE PROFILE
class EmployeeProfile(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE)
    department = models.CharField(max_length=50, choices=DEPARTMENT_CHOICES, default='IT')
    is_department_head = models.BooleanField(default=False)
    # Only relevant when department == 'IT' and not is_department_head
    it_tier = models.CharField(
        max_length=10,
        choices=IT_TIER_CHOICES,
        default='TIER_1',
        blank=True,
        verbose_name="IT Support Tier"
    )

    def __str__(self):
        tier_str = ''
        if self.department == 'IT' and not self.is_department_head:
            tier_str = f' [{self.get_it_tier_display()}]'
        return f"{self.user.username} - {self.get_department_display()} ({'Head' if self.is_department_head else 'Staff'}){tier_str}"

# 3. V2.0 TICKET MODEL (Fully Flowchart Compliant)
class Ticket(models.Model):
    STATUS_CHOICES = [
        ('OPEN', 'Open (Unassigned)'),
        ('IN_PROGRESS', 'In Progress'),
        ('RESOLVED', 'Resolved (Pending Feedback)'),
        ('CLOSED', 'Closed'),
    ]

    TRIAGE_CHOICES = [
        ('LOW', 'Low'),
        ('MEDIUM', 'Medium'),
        ('HIGH', 'High'),
        ('URGENT', 'Urgent'),
    ]

    APPROVAL_CHOICES = [
        ('PENDING', 'Pending Manager Approval'),
        ('APPROVED', 'Approved'),
        ('REJECTED', 'Rejected'),
        ('NOT_REQUIRED', 'Not Required (Emergency)'),
    ]

    # --- TICKET TRACKING ---
    ticket_number = models.CharField(
        max_length=20,
        null=True,
        blank=True,
        unique=True,
        verbose_name="Ticket Number"
    )

    # --- FLOWCHART: APPROVAL STAGE ---
    needs_head_approval = models.BooleanField(default=True, verbose_name="Needs Dept Head Approval")
    approval_status = models.CharField(max_length=20, choices=APPROVAL_CHOICES, default='PENDING')

    # --- WHO REPORTED IT ---
    requester = models.ForeignKey(User, on_delete=models.CASCADE, related_name='submitted_tickets')
    client_name = models.CharField(max_length=100, verbose_name="Affected User/Client Name")
    client_email = models.EmailField(null=True, blank=True)
    department = models.CharField(max_length=50, choices=DEPARTMENT_CHOICES, default='IT')

    # --- THE PROBLEM ---
    title = models.CharField(max_length=200)
    description = models.TextField()
    priority = models.CharField(max_length=20, choices=TRIAGE_CHOICES, default='MEDIUM')

    # --- FLOWCHART: IT PROCESSING & ESCALATION ---
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='OPEN')
    assigned_to = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True, related_name='assigned_tickets'
    )
    is_escalated = models.BooleanField(default=False, verbose_name="Needs Escalation")
    # Track which tier handled/escalated this ticket
    escalated_from_tier = models.CharField(
        max_length=10, choices=IT_TIER_CHOICES, null=True, blank=True,
        verbose_name="Escalated From Tier"
    )

    # --- FLOWCHART: FEEDBACK LOOP ---
    resolution_notes = models.TextField(null=True, blank=True, verbose_name="IT Resolution Notes")
    client_feedback_rating = models.IntegerField(
        null=True, blank=True,
        choices=[(i, i) for i in range(1, 6)],
        verbose_name="Client Star Rating"
    )
    client_feedback_notes = models.TextField(null=True, blank=True, verbose_name="Client Comments")

    # --- TIMESTAMPS ---
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"[{self.get_status_display()}] {self.title} - {self.get_department_display()}"