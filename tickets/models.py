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

# --- ADD THIS TO YOUR Ticket MODEL (Around Line 60) ---
    is_preventive_maintenance = models.BooleanField(default=False, verbose_name="Is Preventive Maintenance")

    # --- FLOWCHART: APPROVAL STAGE ---
    needs_head_approval = models.BooleanField(default=True, verbose_name="Needs Dept Head Approval")
    approval_status = models.CharField(max_length=20, choices=APPROVAL_CHOICES, default='PENDING')

    # --- FLOWCHART: APPROVAL STAGE ---
    needs_head_approval = models.BooleanField(default=True, verbose_name="Needs Dept Head Approval")
    approval_status = models.CharField(max_length=20, choices=APPROVAL_CHOICES, default='PENDING')
    approval_notes = models.TextField(null=True, blank=True, verbose_name="Department Head Instructions")
    
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
    # --- FLOWCHART: IT PROCESSING & ESCALATION ---
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='OPEN')
    assigned_to = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='assigned_tickets')
    is_escalated = models.BooleanField(default=False, verbose_name="Needs Escalation")
    escalated_from_tier = models.CharField(max_length=10, choices=IT_TIER_CHOICES, null=True, blank=True, verbose_name="Escalated From Tier")
    dispatch_notes = models.TextField(null=True, blank=True, verbose_name="IT Dispatcher Instructions")

    # --- FLOWCHART: FEEDBACK LOOP ---
    resolution_notes = models.TextField(null=True, blank=True, verbose_name="IT Resolution Notes")
    client_feedback_rating = models.IntegerField(
        null=True, blank=True,
        choices=[(i, i) for i in range(1, 6)],
        verbose_name="Client Star Rating"
    )
    client_feedback_notes = models.TextField(null=True, blank=True, verbose_name="Client Comments")

    # --- PM REVIEW BY IT HEAD ---
    # After IT Staff resolves a PM ticket, IT Head reviews it before closing.
    source_recurring_task = models.ForeignKey(
        'RecurringTask', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='generated_tickets', verbose_name="Source Recurring Task"
    )
    pm_review_rating = models.IntegerField(
        null=True, blank=True,
        choices=[(i, i) for i in range(1, 6)],
        verbose_name="IT Head PM Review Rating"
    )
    pm_review_notes = models.TextField(null=True, blank=True, verbose_name="IT Head Review Notes")
    pm_reviewed_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='pm_reviews_given', verbose_name="Reviewed By"
    )
    pm_reviewed_at = models.DateTimeField(null=True, blank=True, verbose_name="Reviewed At")

    # --- TIMESTAMPS ---
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"[{self.get_status_display()}] {self.title} - {self.get_department_display()}"

# --- ADD THIS TO THE BOTTOM OF models.py ---

RECURRENCE_CHOICES = [
    ('DAILY', 'Every Day'),
    ('WEEKLY', 'Every Week'),
    ('MONTHLY', 'Every Month'),
    ('QUARTERLY', 'Last Week of Every Quarter'),
    ('CUSTOM', 'Specific Custom Dates'),
]

class RecurringTask(models.Model):
    title = models.CharField(max_length=200)
    description = models.TextField()
    recurrence_type = models.CharField(max_length=20, choices=RECURRENCE_CHOICES)
    
    # For custom date ranges
    custom_date_start = models.DateField(null=True, blank=True)
    custom_date_end = models.DateField(null=True, blank=True)
    
    # Assignment is optional. If blank, it drops into the Unassigned Queue
    assigned_to = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='recurring_tasks')
    priority = models.CharField(max_length=20, default='MEDIUM')
    
    is_active = models.BooleanField(default=True)
    last_generated = models.DateField(null=True, blank=True)
    
    created_by = models.ForeignKey(User, on_delete=models.CASCADE)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.title} ({self.get_recurrence_type_display()})"