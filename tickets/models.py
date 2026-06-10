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

# NEW — Common request categories shown on every ticket submission form.
# "OTHER" is always the last option as a catch-all.
CATEGORY_CHOICES = [
    ('HARDWARE',      'Hardware Issue'),
    ('SOFTWARE',      'Software / Application'),
    ('NETWORK',       'Network & Connectivity'),
    ('ACCOUNT',       'Account & Access'),
    ('EMAIL',         'Email & Communication'),
    ('DATA',          'Data & Backup'),
    ('PERFORMANCE',   'System Performance'),
    ('NEW_EQUIPMENT', 'New Equipment Request'),
    ('INSTALLATION',  'Installation / Setup'),
    ('SECURITY',      'Security Concern'),
    ('OTHER',         'Other'),
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


# 3. TICKET MODEL
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
        max_length=20, null=True, blank=True, unique=True,
        verbose_name="Ticket Number"
    )
    due_date = models.DateField(null=True, blank=True, verbose_name="Deadline")
    is_preventive_maintenance = models.BooleanField(
        default=False, verbose_name="Is Preventive Maintenance"
    )
    

    # Add these two time fields:
    start_time = models.TimeField(null=True, blank=True, verbose_name="Start Time")
    end_time   = models.TimeField(null=True, blank=True, verbose_name="End Time")
    # --- NEW: REQUEST CATEGORY ---
    # Shown as a visual card-picker on every submission form.
    # Defaults to 'OTHER' so existing tickets are never left blank.
    category = models.CharField(
        max_length=20,
        choices=CATEGORY_CHOICES,
        default='OTHER',
        verbose_name="Request Category"
    )

    # --- FLOWCHART: APPROVAL STAGE ---
    needs_head_approval = models.BooleanField(
        default=True, verbose_name="Needs Dept Head Approval"
    )
    approval_status = models.CharField(
        max_length=20, choices=APPROVAL_CHOICES, default='PENDING'
    )
    approval_notes = models.TextField(
        null=True, blank=True, verbose_name="Department Head Instructions"
    )

    # --- WHO REPORTED IT ---
    requester = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name='submitted_tickets'
    )
    client_name = models.CharField(max_length=100, verbose_name="Affected User/Client Name")
    client_email = models.EmailField(null=True, blank=True)
    department = models.CharField(
        max_length=50, choices=DEPARTMENT_CHOICES, default='IT'
    )

    # --- THE PROBLEM ---
    title = models.CharField(max_length=200)
    description = models.TextField()
    priority = models.CharField(
        max_length=20, choices=TRIAGE_CHOICES, default='MEDIUM'
    )

    # --- FLOWCHART: IT PROCESSING & ESCALATION ---
    status = models.CharField(
        max_length=20, choices=STATUS_CHOICES, default='OPEN'
    )
    assigned_to = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='assigned_tickets'
    )
    is_escalated = models.BooleanField(default=False, verbose_name="Needs Escalation")
    escalated_from_tier = models.CharField(
        max_length=10, choices=IT_TIER_CHOICES, null=True, blank=True,
        verbose_name="Escalated From Tier"
    )
    escalated_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='escalated_tickets', verbose_name="Escalated By"
    )
    dispatch_notes = models.TextField(
        null=True, blank=True, verbose_name="IT Dispatcher Instructions"
    )

    # --- FLOWCHART: FEEDBACK LOOP ---
    resolution_notes = models.TextField(
        null=True, blank=True, verbose_name="IT Resolution Notes"
    )
    # ---> ADD THIS LINE <---
    started_at = models.DateTimeField(null=True, blank=True, verbose_name="Started At")
    
    resolved_at = models.DateTimeField(null=True, blank=True, verbose_name="Resolved At")
    client_feedback_rating = models.IntegerField(
        null=True, blank=True,
        choices=[(i, i) for i in range(1, 6)],
        verbose_name="Client Star Rating"
    )
    client_feedback_notes = models.TextField(
        null=True, blank=True, verbose_name="Client Comments"
    )

    # --- PM REVIEW BY IT HEAD ---
    pm_review_rating = models.IntegerField(
        null=True, blank=True,
        choices=[(i, i) for i in range(1, 6)],
        verbose_name="IT Head PM Review Rating"
    )
    pm_review_notes = models.TextField(
        null=True, blank=True, verbose_name="IT Head Review Notes"
    )
    pm_reviewed_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='pm_reviews_given', verbose_name="Reviewed By"
    )
    pm_reviewed_at = models.DateTimeField(
        null=True, blank=True, verbose_name="Reviewed At"
    )

    source_recurring_task = models.ForeignKey(
        'RecurringTask', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='generated_tickets', verbose_name="Source Recurring Task"
    )

    # Add these two time tracking fields:
    scheduled_start_time = models.TimeField(null=True, blank=True, verbose_name="Scheduled Start Time")
    scheduled_end_time   = models.TimeField(null=True, blank=True, verbose_name="Scheduled End Time")

    # --- REOPEN ---
    reopen_reason = models.TextField(null=True, blank=True, verbose_name="Reason for Reopening")
    reopened_at   = models.DateTimeField(null=True, blank=True, verbose_name="Reopened At")
    reopen_count  = models.PositiveIntegerField(default=0, verbose_name="Times Reopened")

    # --- TIMESTAMPS ---
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"[{self.get_status_display()}] {self.title} - {self.get_department_display()}"


# 4. RECURRING TASK (Preventive Maintenance Scheduler)
RECURRENCE_CHOICES = [
    ('DAILY',     'Every Day'),
    ('WEEKLY',    'Every Week'),
    ('MONTHLY',   'Every Month'),
    ('QUARTERLY', 'Last Week of Every Quarter'),
    ('CUSTOM',    'Specific Custom Dates'),
]

class RecurringTask(models.Model):
    title = models.CharField(max_length=200)
    description = models.TextField()
    recurrence_type = models.CharField(max_length=20, choices=RECURRENCE_CHOICES)

    # For custom date ranges
    custom_date_start = models.DateField(null=True, blank=True)
    custom_date_end   = models.DateField(null=True, blank=True)

    # ---> ADD THESE TWO LINES HERE <---
    start_time = models.TimeField(null=True, blank=True, verbose_name="Start Time")
    end_time   = models.TimeField(null=True, blank=True, verbose_name="End Time")

    # Assignment is optional. If blank, it drops into the Unassigned Queue
    assigned_to = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='recurring_tasks'
    )
    priority   = models.CharField(max_length=20, default='MEDIUM')
    is_active  = models.BooleanField(default=True)
    last_generated = models.DateField(null=True, blank=True)

    created_by = models.ForeignKey(User, on_delete=models.CASCADE)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.title} ({self.get_recurrence_type_display()})"

    @property
    def next_due_display(self):
        """Calculates and displays when this PM will generate its next ticket."""
        from datetime import date, timedelta

        if not self.last_generated:
            return "Due Now (Next refresh)"

        today = date.today()

        if self.recurrence_type == 'DAILY':
            if self.last_generated < today:
                return "Due Now"
            return "Tomorrow"

        elif self.recurrence_type == 'WEEKLY':
            start_of_this_week = today - timedelta(days=today.weekday())
            if self.last_generated < start_of_this_week:
                return "Due Now"
            next_monday = start_of_this_week + timedelta(days=7)
            return next_monday.strftime('%b %d, %Y')

        elif self.recurrence_type == 'MONTHLY':
            start_of_this_month = today.replace(day=1)
            if self.last_generated < start_of_this_month:
                return "Due Now"
            if today.month == 12:
                next_month = date(today.year + 1, 1, 1)
            else:
                next_month = date(today.year, today.month + 1, 1)
            return next_month.strftime('%b %d, %Y')

        elif self.recurrence_type == 'QUARTERLY':
            return "Last week of the Quarter"

        elif self.recurrence_type == 'CUSTOM':
            return "One-time schedule"

        return "Unknown"


# 5. NOTIFICATIONS
class Notification(models.Model):
    NOTIF_TYPE_CHOICES = [
        # Employee
        ('ticket_approved',   'Ticket Approved'),
        ('ticket_rejected',   'Ticket Rejected'),
        ('ticket_resolved',   'Ticket Resolved'),
        # Dept Head
        ('needs_approval',    'Ticket Needs Approval'),
        ('ticket_dispatched', 'Ticket Dispatched'),
        # IT Head
        ('needs_assigning',   'Ticket Needs Assigning'),
        ('ticket_reopened',   'Ticket Reopened by Employee'),
        ('pm_finished',       'PM Task Finished'),
        ('it_resolved',       'IT Staff Resolved Ticket'),
        ('rating_received',   'IT Staff Got Rated'),
        # IT Staff
        ('ticket_assigned',   'Ticket Assigned to You'),
        ('pm_announced',      'PM Task Posted'),
        ('feedback_received', 'You Received Feedback'),
        ('ticket_escalated',  'Ticket Escalated to You'),
    ]

    recipient  = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name='notifications'
    )
    notif_type = models.CharField(max_length=30, choices=NOTIF_TYPE_CHOICES)
    title      = models.CharField(max_length=200)
    message    = models.TextField()
    ticket     = models.ForeignKey(
        'Ticket', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='notifications'
    )
    is_read    = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"[{self.notif_type}] → {self.recipient.username}: {self.title}"