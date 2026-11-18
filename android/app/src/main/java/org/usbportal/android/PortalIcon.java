package org.usbportal.android;

import android.graphics.*;
import android.graphics.drawable.Drawable;
import org.usbportal.core.DeviceKind;

/** Small, resolution-independent outline icons; UNKNOWN deliberately resembles a USB-C plug. */
final class PortalIcon extends Drawable {
    private final Paint paint = new Paint(Paint.ANTI_ALIAS_FLAG);
    private final DeviceKind kind;
    private int color;
    PortalIcon(DeviceKind kind, int color) { this.kind = kind; this.color = color; }
    @Override public void draw(Canvas canvas) {
        Rect bounds = getBounds();
        int save = canvas.save();
        float size = Math.min(bounds.width(), bounds.height());
        canvas.translate(bounds.left + (bounds.width() - size) / 2, bounds.top + (bounds.height() - size) / 2);
        canvas.scale(size / 32f, size / 32f);
        paint.setColor(color); paint.setStyle(Paint.Style.STROKE); paint.setStrokeWidth(1.7f);
        paint.setStrokeCap(Paint.Cap.ROUND); paint.setStrokeJoin(Paint.Join.ROUND);
        switch (kind) {
            case GAMEPAD:
                Path controller = new Path();
                controller.moveTo(10, 10); controller.cubicTo(6, 9, 4, 12, 3, 21);
                controller.cubicTo(2, 27, 7, 27, 11, 21); controller.lineTo(21, 21);
                controller.cubicTo(25, 27, 30, 27, 29, 21); controller.cubicTo(28, 12, 26, 9, 22, 10);
                controller.lineTo(10, 10); canvas.drawPath(controller, paint);
                line(canvas, 8, 16, 14, 16); line(canvas, 11, 13, 11, 19);
                dot(canvas, 23, 14, 1.2f); dot(canvas, 26, 17, 1.2f);
                break;
            case KEYBOARD:
                round(canvas, 3, 8, 29, 25, 3);
                for (int y = 12; y <= 17; y += 5) for (int x = 7; x <= 25; x += 4) line(canvas, x, y, x + 1, y);
                line(canvas, 10, 21, 22, 21); break;
            case MOUSE:
                round(canvas, 8, 3, 24, 29, 8); line(canvas, 16, 3, 16, 13); line(canvas, 8, 13, 24, 13);
                line(canvas, 16, 7, 16, 10); break;
            case PRINTER:
                round(canvas, 4, 11, 28, 24, 3); round(canvas, 9, 20, 23, 29, 1);
                line(canvas, 9, 11, 9, 3); line(canvas, 9, 3, 23, 3); line(canvas, 23, 3, 23, 11);
                dot(canvas, 23, 16, 1); line(canvas, 12, 24, 20, 24); break;
            case STORAGE:
                round(canvas, 5, 4, 27, 28, 3); line(canvas, 5, 21, 27, 21);
                dot(canvas, 22, 25, 1); line(canvas, 9, 25, 15, 25); break;
            case CAMERA:
                Path camera = new Path(); camera.moveTo(4, 10); camera.lineTo(10, 10); camera.lineTo(12, 6);
                camera.lineTo(20, 6); camera.lineTo(22, 10); camera.lineTo(28, 10); camera.lineTo(28, 26);
                camera.lineTo(4, 26); camera.close(); canvas.drawPath(camera, paint);
                canvas.drawCircle(16, 18, 5, paint); break;
            case AUDIO:
                canvas.drawArc(new RectF(5, 4, 27, 27), 180, 180, false, paint);
                round(canvas, 4, 16, 10, 27, 2); round(canvas, 22, 16, 28, 27, 2); break;
            case HUB:
                round(canvas, 9, 12, 23, 22, 2); line(canvas, 16, 5, 16, 12);
                line(canvas, 5, 27, 5, 18); line(canvas, 5, 18, 9, 18);
                line(canvas, 27, 27, 27, 18); line(canvas, 27, 18, 23, 18);
                line(canvas, 16, 22, 16, 28); break;
            default:
                // Front-facing rounded USB-C shell, inner tongue, contacts, and short cable neck.
                round(canvas, 4, 5, 28, 21, 7); round(canvas, 8, 9, 24, 17, 3);
                line(canvas, 11, 12, 21, 12); line(canvas, 12, 21, 12, 27);
                line(canvas, 20, 21, 20, 27); line(canvas, 12, 27, 20, 27); break;
        }
        canvas.restoreToCount(save);
    }
    private void round(Canvas c, float l, float t, float r, float b, float radius) {
        c.drawRoundRect(l, t, r, b, radius, radius, paint);
    }
    private void line(Canvas c, float x1, float y1, float x2, float y2) { c.drawLine(x1, y1, x2, y2, paint); }
    private void dot(Canvas c, float x, float y, float r) {
        paint.setStyle(Paint.Style.FILL); c.drawCircle(x, y, r, paint); paint.setStyle(Paint.Style.STROKE);
    }
    @Override public void setAlpha(int alpha) { color = (color & 0x00ffffff) | (alpha << 24); invalidateSelf(); }
    @Override public void setColorFilter(ColorFilter filter) { paint.setColorFilter(filter); invalidateSelf(); }
    @Override public int getOpacity() { return PixelFormat.TRANSLUCENT; }
}
