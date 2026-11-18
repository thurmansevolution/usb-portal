package org.usbportal.android;

import android.graphics.*;
import android.graphics.drawable.Drawable;

/** Static atmosphere and glass highlights, without framebuffer blur or animation work. */
final class PortalSurface extends Drawable {
    private final Paint paint = new Paint(Paint.ANTI_ALIAS_FLAG);
    @Override public void draw(Canvas canvas) {
        Rect b = getBounds();
        paint.setShader(new LinearGradient(0, b.top, b.right, b.bottom,
                new int[] {0xff101923, 0xff090f16, 0xff101820}, null, Shader.TileMode.CLAMP));
        canvas.drawRect(b, paint);
        float radius = Math.max(b.width(), b.height()) * .72f;
        paint.setShader(new RadialGradient(b.right * .87f, b.top - b.height() * .06f, radius,
                new int[] {0x28436b7e, 0x00344957}, null, Shader.TileMode.CLAMP));
        canvas.drawRect(b, paint);
        paint.setShader(new RadialGradient(b.left, b.bottom * 1.14f, radius * .65f,
                new int[] {0x15376a62, 0x0015242d}, null, Shader.TileMode.CLAMP));
        canvas.drawRect(b, paint); paint.setShader(null);
    }
    @Override public void setAlpha(int alpha) { }
    @Override public void setColorFilter(ColorFilter filter) { }
    @Override public int getOpacity() { return PixelFormat.OPAQUE; }
}
