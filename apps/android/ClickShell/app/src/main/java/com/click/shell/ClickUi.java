package com.click.shell;

import android.app.Activity;
import android.content.Context;
import android.content.res.ColorStateList;
import android.graphics.Color;
import android.graphics.Typeface;
import android.graphics.drawable.Drawable;
import android.graphics.drawable.GradientDrawable;
import android.graphics.drawable.RippleDrawable;
import android.view.Gravity;
import android.view.View;
import android.widget.Button;
import android.widget.TextView;

import androidx.annotation.ColorRes;
import androidx.annotation.DrawableRes;
import androidx.appcompat.content.res.AppCompatResources;
import androidx.core.content.ContextCompat;
import androidx.core.graphics.drawable.DrawableCompat;

final class ClickUi {
    private ClickUi() {
    }

    static int canvas(Context context, boolean dark) {
        return color(context, dark, R.color.click_canvas_light, R.color.click_canvas_dark);
    }

    static int surface(Context context, boolean dark) {
        return color(context, dark, R.color.click_surface_light, R.color.click_surface_dark);
    }

    static int elevated(Context context, boolean dark) {
        return color(context, dark, R.color.click_surface_elevated_light, R.color.click_surface_elevated_dark);
    }

    static int primaryLabel(Context context, boolean dark) {
        return color(context, dark, R.color.click_label_primary_light, R.color.click_label_primary_dark);
    }

    static int secondaryLabel(Context context, boolean dark) {
        return color(context, dark, R.color.click_label_secondary_light, R.color.click_label_secondary_dark);
    }

    static int separator(Context context, boolean dark) {
        return color(context, dark, R.color.click_separator_light, R.color.click_separator_dark);
    }

    static int accent(Context context, boolean dark) {
        return color(context, dark, R.color.click_accent_light, R.color.click_accent_dark);
    }

    static int warning(Context context, boolean dark) {
        return color(context, dark, R.color.click_warning_light, R.color.click_warning_dark);
    }

    static int destructive(Context context, boolean dark) {
        return color(context, dark, R.color.click_destructive_light, R.color.click_destructive_dark);
    }

    static GradientDrawable rounded(int color, float radius, int strokeColor, int strokeWidth) {
        GradientDrawable drawable = new GradientDrawable();
        drawable.setColor(color);
        drawable.setCornerRadius(radius);
        if (strokeWidth > 0) {
            drawable.setStroke(strokeWidth, strokeColor);
        }
        return drawable;
    }

    static Drawable ripple(int fillColor, float radius, int strokeColor, int strokeWidth, int accentColor) {
        GradientDrawable content = rounded(fillColor, radius, strokeColor, strokeWidth);
        int rippleColor = Color.argb(48, Color.red(accentColor), Color.green(accentColor), Color.blue(accentColor));
        return new RippleDrawable(ColorStateList.valueOf(rippleColor), content, null);
    }

    static void styleNavigationTitle(TextView view, boolean dark) {
        view.setTextColor(primaryLabel(view.getContext(), dark));
        view.setTextSize(20);
        view.setTypeface(Typeface.DEFAULT, Typeface.BOLD);
        view.setGravity(Gravity.CENTER_VERTICAL);
    }

    static void styleBody(TextView view, boolean dark) {
        view.setTextColor(primaryLabel(view.getContext(), dark));
        view.setTextSize(16);
        view.setTypeface(Typeface.DEFAULT, Typeface.NORMAL);
    }

    static void styleSecondary(TextView view, boolean dark) {
        view.setTextColor(secondaryLabel(view.getContext(), dark));
        view.setTextSize(13);
        view.setTypeface(Typeface.DEFAULT, Typeface.NORMAL);
    }

    static void styleIconButton(
            Button button,
            @DrawableRes int iconRes,
            String description,
            boolean dark
    ) {
        Context context = button.getContext();
        Drawable icon = AppCompatResources.getDrawable(context, iconRes);
        if (icon != null) {
            icon = DrawableCompat.wrap(icon.mutate());
            DrawableCompat.setTint(icon, primaryLabel(context, dark));
            int size = dp(context, 22);
            icon.setBounds(0, 0, size, size);
        }
        button.setText("");
        button.setContentDescription(description);
        button.setCompoundDrawables(icon, null, null, null);
        button.setGravity(Gravity.CENTER);
        button.setMinWidth(0);
        button.setMinimumWidth(0);
        button.setMinHeight(0);
        button.setMinimumHeight(0);
        button.setPadding(0, 0, 0, 0);
        button.setBackground(ripple(Color.TRANSPARENT, dp(context, 24), Color.TRANSPARENT, 0, accent(context, dark)));
    }

    static void stylePill(Button button, boolean selected, boolean dark) {
        Context context = button.getContext();
        int fill = selected ? primaryLabel(context, dark) : elevated(context, dark);
        int foreground = selected ? canvas(context, dark) : primaryLabel(context, dark);
        int stroke = selected ? fill : separator(context, dark);
        button.setAllCaps(false);
        button.setTextSize(14);
        button.setTypeface(Typeface.DEFAULT, selected ? Typeface.BOLD : Typeface.NORMAL);
        button.setTextColor(foreground);
        button.setMinHeight(dp(context, 40));
        button.setPadding(dp(context, 16), 0, dp(context, 16), 0);
        button.setBackground(ripple(fill, dp(context, 20), stroke, dp(context, 1), accent(context, dark)));
    }

    static void stylePrimary(Button button, boolean dark) {
        Context context = button.getContext();
        button.setAllCaps(false);
        button.setTextSize(16);
        button.setTypeface(Typeface.DEFAULT, Typeface.BOLD);
        button.setTextColor(Color.WHITE);
        button.setMinHeight(dp(context, 48));
        button.setBackground(ripple(
                accent(context, dark),
                dp(context, 24),
                accent(context, dark),
                0,
                Color.WHITE
        ));
    }

    static void styleSecondaryButton(Button button, boolean dark) {
        Context context = button.getContext();
        button.setAllCaps(false);
        button.setTextSize(15);
        button.setTypeface(Typeface.DEFAULT, Typeface.NORMAL);
        button.setTextColor(primaryLabel(context, dark));
        button.setMinHeight(dp(context, 44));
        button.setBackground(ripple(
                elevated(context, dark),
                dp(context, 16),
                separator(context, dark),
                dp(context, 1),
                accent(context, dark)
        ));
    }

    static void tintTree(View view, boolean dark) {
        if (view instanceof TextView) {
            ((TextView) view).setTextColor(primaryLabel(view.getContext(), dark));
        }
        if (view instanceof android.view.ViewGroup) {
            android.view.ViewGroup group = (android.view.ViewGroup) view;
            for (int index = 0; index < group.getChildCount(); index++) {
                tintTree(group.getChildAt(index), dark);
            }
        }
    }

    static void applySystemBars(Activity activity, boolean dark) {
        int background = canvas(activity, dark);
        activity.getWindow().setStatusBarColor(background);
        activity.getWindow().setNavigationBarColor(background);
        View decor = activity.getWindow().getDecorView();
        int flags = decor.getSystemUiVisibility();
        if (dark) {
            flags &= ~View.SYSTEM_UI_FLAG_LIGHT_STATUS_BAR;
            flags &= ~View.SYSTEM_UI_FLAG_LIGHT_NAVIGATION_BAR;
        } else {
            flags |= View.SYSTEM_UI_FLAG_LIGHT_STATUS_BAR;
            flags |= View.SYSTEM_UI_FLAG_LIGHT_NAVIGATION_BAR;
        }
        decor.setSystemUiVisibility(flags);
    }

    static int dp(Context context, int value) {
        return Math.round(value * context.getResources().getDisplayMetrics().density);
    }

    private static int color(
            Context context,
            boolean dark,
            @ColorRes int lightColor,
            @ColorRes int darkColor
    ) {
        return ContextCompat.getColor(context, dark ? darkColor : lightColor);
    }
}
