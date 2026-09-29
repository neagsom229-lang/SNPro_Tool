from flask_wtf import FlaskForm
from wtforms import StringField, PasswordField, BooleanField, SubmitField
from wtforms.validators import DataRequired, Email, Length, EqualTo, Regexp, ValidationError

MIN_PASSWORD_LENGTH = 8
MAX_PASSWORD_LENGTH = 128


def validate_strong_password(form, field):
    password = field.data
    if not password:
        return
    has_upper = any(c.isupper() for c in password)
    has_lower = any(c.islower() for c in password)
    has_digit = any(c.isdigit() for c in password)
    has_symbol = any(not c.isalnum() for c in password)

    if not (has_upper and has_lower and has_digit and has_symbol):
        raise ValidationError(
            "Password must contain at least one uppercase letter, one lowercase letter, "
            "one number, and one special character."
        )


class RegisterForm(FlaskForm):
    username = StringField(
        "Username",
        validators=[
            DataRequired(),
            Length(min=3, max=64),
            Regexp(r'^[A-Za-z0-9_-]+$',
                   message="Letters, numbers, underscores, and hyphens only."),
        ]
    )
    full_name = StringField("Full Name", validators=[Length(max=120)])
    email = StringField("Email", validators=[DataRequired(), Email(), Length(max=150)])
    password = PasswordField(
        "Password",
        validators=[
            DataRequired(),
            Length(min=MIN_PASSWORD_LENGTH, max=MAX_PASSWORD_LENGTH,
                   message=f"Password must be at least {MIN_PASSWORD_LENGTH} characters."),
            validate_strong_password,
        ]
    )
    confirm = PasswordField(
        "Confirm Password",
        validators=[DataRequired(), EqualTo("password", message="Passwords must match")]
    )
    submit = SubmitField("Create Account")


class LoginForm(FlaskForm):
    username = StringField("Username or Email", validators=[DataRequired()])
    password = PasswordField("Password", validators=[DataRequired()])
    remember = BooleanField("Remember me")
    submit = SubmitField("Sign In")


class ForgotPasswordForm(FlaskForm):
    email = StringField("Email Address", validators=[DataRequired(), Email(), Length(max=150)])
    submit = SubmitField("Send Reset Link")


class ResetPasswordForm(FlaskForm):
    password = PasswordField(
        "New Password",
        validators=[
            DataRequired(),
            Length(min=MIN_PASSWORD_LENGTH, max=MAX_PASSWORD_LENGTH,
                   message=f"Password must be at least {MIN_PASSWORD_LENGTH} characters."),
            validate_strong_password,
        ]
    )
    confirm = PasswordField(
        "Confirm New Password",
        validators=[DataRequired(), EqualTo("password", message="Passwords must match")]
    )
    submit = SubmitField("Reset Password")


class ChangePasswordForm(FlaskForm):
    current_password = PasswordField("Current Password", validators=[DataRequired()])
    new_password = PasswordField(
        "New Password",
        validators=[
            DataRequired(),
            Length(min=MIN_PASSWORD_LENGTH, max=MAX_PASSWORD_LENGTH,
                   message=f"Password must be at least {MIN_PASSWORD_LENGTH} characters."),
            validate_strong_password,
        ]
    )
    confirm_new = PasswordField(
        "Confirm New Password",
        validators=[DataRequired(), EqualTo("new_password", message="Passwords must match")]
    )
    submit = SubmitField("Update Password")
